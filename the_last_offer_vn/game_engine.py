"""
Game engine for The Last Offer — Visual Novel edition.
Imports core logic from the Gradio version, wraps it in an event-based interface
for WebSocket communication.
"""
import sys, os, re, asyncio

# ── Import game logic from parent module ──────────────────────────────────
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _parent)

from the_last_offer_v4_25 import (
    INVESTORS, PERSONALITY_PREFIX, QUESTION_STYLE_GUIDANCE, MODEL, LIGHT_MODEL,
    groq_call, classify_answer, score_answer_local, parse_score_from_response,
    disposition_modifier, validate_pitch, extract_claims, extract_ask,
    extract_deal_action, parse_investor_offer, build_deal_signal,
    build_difficulty_context, generate_startup,
    new_state, strip_hedges, extract as extract_label,
    SKIP_EXITS, SOCIAL_REACTIONS, DIFFICULTY_PROFILES, SCORE_MAP,
)

# ── Helpers ────────────────────────────────────────────────────────────────

def ev(type, **data):
    """Create an event dict."""
    return {'type': type, **data}


def _inv_key(name):
    """Normalize investor name to a CSS/file-safe key."""
    return name.lower().replace(' ', '_').replace('.', '')


def inv_info(inv):
    """Serializable investor info."""
    return {
        'name': inv['name'], 'title': inv['title'],
        'thesis': inv['thesis'], 'accent': inv['accent'],
        'key': _inv_key(inv['name']),
    }


def pills_data(round_idx, verdicts):
    """Build pills list for the frontend."""
    pills = []
    for i, inv in enumerate(INVESTORS):
        if i < len(verdicts):
            pills.append({
                'name': inv['name'], 'accent': inv['accent'],
                'status': 'invest' if verdicts[i]['decision'] == 'INVEST' else 'pass',
            })
        elif i == round_idx:
            pills.append({'name': inv['name'], 'accent': inv['accent'], 'status': 'active'})
        else:
            pills.append({'name': inv['name'], 'accent': inv['accent'], 'status': 'waiting'})
    return pills


def get_expression(disposition, answer_type=None, is_verdict=False, decision=None):
    """Pick character expression based on game state."""
    if is_verdict:
        return 'impressed' if decision == 'INVEST' else 'skeptical'
    if answer_type == 'INSULT':
        return 'angry'
    if answer_type == 'CHARM':
        return 'impressed'
    if disposition >= 2:
        return 'impressed'
    if disposition >= 0:
        return 'neutral'
    if disposition >= -1:
        return 'thinking'
    return 'skeptical'


def clean_response(resp):
    """Strip internal LLM tags from response for display."""
    d = resp
    d = re.sub(r'SCORE:\s*(?:STRONG|SOLID|VAGUE|EVASIVE|DODGE)\s*\n?', '', d, flags=re.IGNORECASE)
    d = re.sub(r'QUESTION:\s*', '', d, flags=re.IGNORECASE)
    d = re.sub(r'VERDICT:\s*(?:INVEST|PASS)\s*\n?', '', d, flags=re.IGNORECASE)
    d = re.sub(r'REASON:\s*', '', d, flags=re.IGNORECASE)
    d = re.sub(r'OFFER:\s*', '', d, flags=re.IGNORECASE)
    return d.strip()


def _offer_terms_text(offer):
    """Build a short human-readable string for an offer."""
    if not offer:
        return 'the agreed terms'
    parts = []
    if offer.get('amount'):
        parts.append(str(offer['amount']))
    if offer.get('equity'):
        parts.append(f"for {offer['equity']}")
    return ' '.join(parts) if parts else 'the agreed terms'


def _last_question(hist, ri):
    """Extract the most recent investor question from history."""
    for msg in reversed(hist[ri]):
        if msg.get('role') == 'assistant':
            q = extract_label(msg['content'], 'QUESTION')
            return q if q else msg['content'][:200]
    return ''


# ── Game Session ───────────────────────────────────────────────────────────

class GameSession:
    """State-machine game engine. Each action returns a list of event dicts."""

    def __init__(self):
        self.state = new_state()

    async def handle(self, action, data=None):
        data = data or {}
        try:
            if action == 'start':
                return self._start()
            elif action == 'select_mode':
                return await self._select_mode(data.get('mode', 'custom'))
            elif action == 'submit_text':
                return await self._submit_text(data.get('text', ''))
            elif action == 'skip':
                return await self._skip()
            elif action == 'continue':
                return await self._continue()
            elif action == 'replay':
                return await self._replay()
            elif action == 'go_anyway':
                return await self._go_anyway()
            elif action == 'pitch_again':
                return self._pitch_again()
        except Exception as e:
            return [ev('error', message=str(e))]
        return []

    # ── Screens ────────────────────────────────────────────────────────────

    def _start(self):
        self.state = new_state()
        return [ev('scene', screen='mode_select'), ev('music', track='menu')]

    async def _finalize_accepted_offer(self, events, founder_text, ri, inv, disp, offer):
        """Immediately close the current round when the founder accepts this investor's offer."""
        s = self.state
        hist = s['histories']
        terms = _offer_terms_text(offer)
        close_line = f"Deal. {terms}. I'm in."
        reason = f"Accepted {terms}."
        hist[ri] = hist[ri] + [
            {'role': 'user', 'content': founder_text},
            {'role': 'assistant', 'content': close_line},
        ]
        s['histories'] = hist
        return await self._deliver_verdict(
            events, ri, inv, disp, 'INVEST', reason, close_line, display_text=close_line
        )

    async def _select_mode(self, mode):
        s = self.state
        events = []

        if mode == 'custom':
            s['game_mode'] = 'custom'
            s['phase'] = 'setup'
            events.append(ev('scene', screen='pitch'))
            events.append(ev('input', enabled=True,
                             placeholder='Describe your startup — what it does, who it\'s for, why now...'))
            return events

        # Generated mode
        s['game_mode'] = 'generated'
        s['difficulty'] = mode
        profile = DIFFICULTY_PROFILES[mode]
        s['dispositions'] = [profile.get('starting_disposition', 0)] * 4

        events.append(ev('scene', screen='loading'))
        brief = await generate_startup(mode)
        s['startup_brief'] = brief
        s['phase'] = 'briefing'

        events.append(ev('scene', screen='briefing'))
        events.append(ev('briefing', text=brief, difficulty=mode, label=profile['label']))
        return events

    async def _submit_text(self, text):
        phase = self.state['phase']
        if phase == 'briefing':
            # Generated mode can skip manual pitch entry and use the brief directly.
            if self.state.get('game_mode') == 'generated':
                auto_pitch = (self.state.get('startup_brief') or '').strip()
                if auto_pitch:
                    self.state['pitch'] = auto_pitch
                    self.state['pending_pitch'] = auto_pitch
                    return await self._enter_questioning(auto_pitch)
            self.state['phase'] = 'setup'
            return [
                ev('scene', screen='pitch'),
                ev('input', enabled=True,
                   placeholder='Write your pitch in your own words — the investors know the business, they\'re judging YOU...'),
            ]
        if phase == 'setup':
            return await self._handle_setup(text)
        if phase == 'warning':
            self.state['pending_pitch'] = text
            return await self._handle_setup(text)
        if phase == 'questioning':
            return await self._handle_questioning(text)
        if phase == 'verdict' and self.state.get('post_verdict_mode'):
            return await self._handle_post_verdict(text)
        return []

    async def _handle_setup(self, text):
        s = self.state
        if not text.strip():
            return []
        s['pitch'] = text
        s['pending_pitch'] = text

        valid, missing, feedback = await validate_pitch(text)
        if not valid and missing:
            s['phase'] = 'warning'
            return [
                ev('scene', screen='warning'),
                ev('warning', missing=missing, feedback=feedback),
            ]

        return await self._enter_questioning(text)

    async def _enter_questioning(self, pitch):
        s = self.state
        s['phase'] = 'questioning'
        s['round'] = 0
        s['messages'] = []
        ri = 0
        inv = INVESTORS[ri]
        events = []

        diff_ctx = build_difficulty_context(s)
        system_msg = (PERSONALITY_PREFIX + '\n\n' + inv['system'] + diff_ctx).strip()
        msgs = [
            {'role': 'system', 'content': system_msg},
            {'role': 'user', 'content': (
                f"Startup pitch:\n\n{pitch}\n\n"
                "Ask your first question.\n\n"
                f"{QUESTION_STYLE_GUIDANCE}"
            )},
        ]

        events.append(ev('scene', screen='game'))
        events.append(ev('music', track='pitch'))
        events.append(ev('pills', data=pills_data(ri, s['verdicts'])))
        events.append(ev('investor', **inv_info(inv)))
        events.append(ev('character', key=_inv_key(inv['name']), expression='neutral'))
        events.append(ev('thinking'))

        resp = await groq_call(msgs, temperature=inv['temp'], max_tokens=320)
        q = extract_label(resp, 'QUESTION')
        s['histories'][ri] = msgs + [{'role': 'assistant', 'content': resp}]
        s['turns'][ri] = 1
        s['messages'].append({'role': 'assistant', 'content': q})

        expr = get_expression(s['dispositions'][ri])
        events.append(ev('character', key=_inv_key(inv['name']), expression=expr))
        events.append(ev('dialogue', speaker=inv['name'], text=q, accent=inv['accent']))
        events.append(ev('disposition', score=s['dispositions'][ri], name=inv['name']))
        events.append(ev('input', enabled=True, placeholder='Your answer...'))
        events.append(ev('buttons', buttons=[
            {'id': 'skip', 'label': f'Skip {inv["name"]}', 'variant': 'ghost'}
        ]))
        return events

    # ── Questioning (main game loop) ───────────────────────────────────────

    async def _handle_questioning(self, text):
        s = self.state
        events = []
        if not text.strip():
            return []

        ri = s['round']
        inv = INVESTORS[min(ri, 3)]
        hist = s['histories']

        s['messages'].append({'role': 'user', 'content': text})
        events.append(ev('user_message', text=text))

        turn = s['turns'][ri]

        last_q = _last_question(hist, ri)

        # Classify answer
        answer_type = await classify_answer(last_q, text)

        # Social reactions (charm / insult)
        if answer_type in ('CHARM', 'INSULT'):
            reaction = SOCIAL_REACTIONS[inv['name']][answer_type]
            social_prompt = (
                f'The founder said: "{text}"\n\n'
                f'{reaction}\n\n'
                'IMPORTANT: Do NOT repeat your previous question verbatim. '
                'Rephrase it — same intent, completely different wording.'
            )
            msgs = hist[ri] + [
                {'role': 'user', 'content': text},
                {'role': 'user', 'content': social_prompt},
            ]
            temp_boost = 0.15 if answer_type == 'INSULT' else 0.1
            expr = 'angry' if answer_type == 'INSULT' else 'impressed'
            events.append(ev('character', key=_inv_key(inv['name']), expression=expr))
            events.append(ev('thinking'))

            resp = await groq_call(msgs, temperature=min(inv['temp'] + temp_boost, 1.0))
            # Use clean_response so VERDICT:/REASON: never appear in the speech bubble
            display = clean_response(resp)
            hist[ri] = msgs + [{'role': 'assistant', 'content': resp}]
            s['histories'] = hist
            s['messages'].append({'role': 'assistant', 'content': display})

            # If the investor decided to verdict mid-reaction, honour it
            verdict_match = re.search(r'VERDICT:\s*(INVEST|PASS)', resp, re.IGNORECASE)
            if verdict_match:
                decision = verdict_match.group(1).upper()
                disp = s['dispositions'][ri]
                reason = strip_hedges(extract_label(resp, 'REASON') or display.split('\n')[0])
                events.append(ev('dialogue', speaker=inv['name'], text=display, accent=inv['accent']))
                return await self._deliver_verdict(events, ri, inv, disp, decision, reason, resp)

            events.append(ev('dialogue', speaker=inv['name'], text=display, accent=inv['accent']))
            events.append(ev('input', enabled=True, placeholder='Your answer...'))
            events.append(ev('buttons', buttons=[
                {'id': 'skip', 'label': f'Skip {inv["name"]}', 'variant': 'ghost'}
            ]))
            return events

        # Score answer
        q_score = score_answer_local(last_q, text)
        s['dispositions'][ri] += q_score
        disp = s['dispositions'][ri]

        # Early exit (very bad disposition)
        early_exit_ok = (
            disp <= -3 and turn >= 2
            and not (s['ask_established'] and inv['name'] not in s['offers'])
        )
        if early_exit_ok:
            return await self._force_verdict(text, ri, inv, disp)

        # ── Deal detection ──
        if not s['ask_established']:
            ask_result = extract_ask(text)
            if ask_result.get('found'):
                s['ask']['amount'] = ask_result.get('amount')
                s['ask']['equity'] = ask_result.get('equity')
                s['ask']['valuation'] = ask_result.get('valuation')
                has_info = ask_result.get('amount') or ask_result.get('equity') or ask_result.get('valuation')
                if has_info:
                    s['ask_established'] = True
                    if turn >= 2:
                        s['deal_phase'] = 'valuation'

        if s['offers']:
            deal_action = await extract_deal_action(inv['name'], inv['title'], text, s)
            if deal_action['action'] == 'accept':
                target = deal_action.get('investor') or s.get('active_deal')
                if target and target in s['offers']:
                    s['offers'][target]['status'] = 'accepted'
                    s['deal_phase'] = 'closed'
                    if target == inv['name']:
                        return await self._finalize_accepted_offer(
                            events, text, ri, inv, disp, s['offers'][target]
                        )
            elif deal_action['action'] == 'reject':
                target = deal_action.get('investor') or s.get('active_deal')
                if target and target in s['offers']:
                    s['offers'][target]['status'] = 'rejected'
                    s['active_deal'] = None
            elif deal_action['action'] == 'counter':
                target = deal_action.get('investor') or s.get('active_deal')
                if target and target in s['offers']:
                    s['offers'][target]['status'] = 'countered'
                    s['offers'][target]['counter'] = {
                        'amount': deal_action.get('counter_amount'),
                        'equity': deal_action.get('counter_equity'),
                    }

        if s['ask_established'] and s['deal_phase'] == 'exploring' and turn >= 3:
            s['deal_phase'] = 'valuation'

        # ── Build prompt ──
        deal_signal = build_deal_signal(s, inv, turn, disp)
        tone = disposition_modifier(disp, inv['name'])
        mood_line = f'CURRENT MOOD: {tone}\n\n' if tone else ''
        max_turns = 8

        must_offer_first = (
            s['ask_established']
            and s['deal_phase'] in ('valuation', 'negotiating')
            and inv['name'] not in s['offers']
        )
        investor_has_offer = inv['name'] in s['offers']
        effective_max = max_turns + 2 if (s['ask_established'] and not investor_has_offer) else max_turns

        next_prompt = None

        if turn >= effective_max:
            force_deal = (
                s['ask_established'] and not investor_has_offer
                and turn < max_turns + 2
            )
            if not force_deal:
                # Force verdict
                ask = s['ask']
                ask_ctx = ''
                if s['ask_established']:
                    parts = []
                    if ask.get('amount'): parts.append(ask['amount'])
                    if ask.get('equity'): parts.append(f"for {ask['equity']}")
                    if ask.get('valuation'): parts.append(f"implying {ask['valuation']} valuation")
                    if parts:
                        ask_ctx = (f" The founder asked for {' '.join(parts)}."
                                   " Weigh that ask against what you actually heard —"
                                   " attractive terms on a solid business means INVEST.")
                next_prompt = (
                    f"{mood_line}"
                    f'They just said: "{text}"\n\n'
                    f"You've had a full conversation. Time to decide.{ask_ctx}\n"
                    "If what you heard gave you conviction, INVEST — real investors take good deals.\n"
                    "If something specific and unresolved is blocking you, PASS — name it exactly.\n"
                    "VERDICT: INVEST or PASS\n"
                    "REASON: one sentence, in your own voice, specific to this conversation."
                )
            else:
                must_offer_first = True

        if next_prompt is None:
            deal_line = f'\n\n{deal_signal}' if deal_signal else ''
            ask_missing_guard = ''
            if not s['ask_established']:
                ask_missing_guard = (
                    "\nThe founder has NOT explicitly stated an investment ask yet. "
                    "Do NOT invent a check size, equity percentage, valuation, or offer terms. "
                    "If you want to discuss a deal, ask what they are raising and for how much equity first."
                )
            if must_offer_first:
                ask = s['ask']
                parts = []
                if ask.get('amount'): parts.append(ask['amount'])
                if ask.get('equity'): parts.append(f"for {ask['equity']}")
                ask_str = ' '.join(parts) if parts else 'their ask'
                next_prompt = (
                    f"{mood_line}"
                    f'They just said: "{text}"\n\n'
                    "React to what they actually said — say what you're thinking."
                    f"{deal_line}\n"
                    f"The founder is asking for {ask_str}. "
                    "You MUST respond with a deal position before you can give a verdict. "
                    "This is how real investing works — you negotiate, THEN you decide.\n\n"
                    "Before your response, rate the answer (hidden from founder):\n"
                    "SCORE: STRONG / SOLID / VAGUE / EVASIVE / DODGE\n\n"
                    "Your options:\n"
                    "- OFFER: [amount] for [equity%] — make your offer\n"
                    "- I'M OUT — walk away\n"
                    "- Ask ONE more question if something specific is still unresolved\n\n"
                    "Do NOT skip straight to VERDICT. Negotiate first.\n\n"
                    f"{QUESTION_STYLE_GUIDANCE}"
                )
            else:
                ask = s['ask']
                ask_reminder = ''
                if s['ask_established'] and s['deal_phase'] != 'exploring':
                    parts = []
                    if ask.get('amount'): parts.append(ask['amount'])
                    if ask.get('equity'): parts.append(f"for {ask['equity']}")
                    if parts:
                        ask_reminder = (f"\nNote: the founder is asking {' '.join(parts)}."
                                        " If you issue a VERDICT, weigh the price against"
                                        " what you actually heard — good traction at a low price is a reason to INVEST.")
                next_prompt = (
                    f"{mood_line}"
                    f'They just said: "{text}"\n\n'
                    "React to what they actually said. Say what you're thinking. "
                    "Ask what you genuinely want to know next. One thing at a time."
                    f"{deal_line}{ask_reminder}{ask_missing_guard}\n"
                    "Before your response, rate the answer (hidden from founder):\n"
                    "SCORE: STRONG / SOLID / VAGUE / EVASIVE / DODGE\n\n"
                    "QUESTION: [what you want to know]\n"
                    "— or, if you've heard enough —\n"
                    "VERDICT: INVEST or PASS\n"
                    "REASON: [one sentence, specific to what was said]\n\n"
                    f"{QUESTION_STYLE_GUIDANCE}"
                )

        # ── LLM call ──
        msgs = hist[ri] + [
            {'role': 'user', 'content': text},
            {'role': 'user', 'content': next_prompt},
        ]
        events.append(ev('thinking'))
        resp = await groq_call(msgs, temperature=inv['temp'], max_tokens=320)
        hist[ri] = msgs + [{'role': 'assistant', 'content': resp}]
        s['histories'] = hist

        # Refine score
        inv_score = parse_score_from_response(resp)
        if inv_score is not None:
            s['dispositions'][ri] = s['dispositions'][ri] - q_score + inv_score
            disp = s['dispositions'][ri]

        # Parse deal declarations
        deal_decl = parse_investor_offer(inv['name'], resp)
        if 'offer' in deal_decl and not s['ask_established']:
            deal_decl.pop('offer', None)
            q = "Before we talk terms, what exactly are you raising and what equity are you offering?"
            if hist[ri] and hist[ri][-1].get('role') == 'assistant':
                hist[ri][-1]['content'] = q
                s['histories'] = hist
            s['turns'][ri] += 1
            s['total_turns'] += 1
            s['messages'].append({'role': 'assistant', 'content': q})
            expr = get_expression(disp)
            events.append(ev('character', key=_inv_key(inv['name']), expression=expr))
            events.append(ev('dialogue', speaker=inv['name'], text=q, accent=inv['accent']))
            events.append(ev('disposition', score=disp, name=inv['name']))
            events.append(ev('input', enabled=True, placeholder='Your answer...'))
            events.append(ev('buttons', buttons=[
                {'id': 'skip', 'label': f'Skip {inv["name"]}', 'variant': 'ghost'}
            ]))
            if s['offers']:
                events.append(ev('deal_status', ask=s['ask'], offers={
                    k: {kk: vv for kk, vv in v.items() if kk != 'raw'}
                    for k, v in s['offers'].items()
                }))
            return events
        if 'offer' in deal_decl:
            s['offers'][inv['name']] = {
                'amount': None, 'equity': None,
                'raw': deal_decl['offer'], 'status': 'open',
            }
            amt_m = re.search(r'([£$€][\d,.]+[km]?|[\d,.]+\s*(?:k|million|m))', deal_decl['offer'], re.IGNORECASE)
            eq_m = re.search(r'(\d+(?:\.\d+)?)\s*(?:%|percent)', deal_decl['offer'], re.IGNORECASE)
            if amt_m: s['offers'][inv['name']]['amount'] = amt_m.group(1)
            if eq_m: s['offers'][inv['name']]['equity'] = eq_m.group(1) + '%'
            s['active_deal'] = inv['name']
            if s['deal_phase'] in ('exploring', 'valuation'):
                s['deal_phase'] = 'negotiating'
        if 'out' in deal_decl and inv['name'] not in s['offers']:
            s['offers'][inv['name']] = {'status': 'rejected', 'raw': deal_decl['out']}
        if 'joint_proposal' in deal_decl:
            s['partnership_proposal'] = {'proposer': inv['name'], 'partner': deal_decl['joint_proposal']}
        if deal_decl.get('counter_accept') and s.get('active_deal'):
            target = s['active_deal']
            if target in s['offers']:
                counter = s['offers'][target].get('counter', {})
                s['offers'][target]['amount'] = counter.get('amount', s['offers'][target].get('amount'))
                s['offers'][target]['equity'] = counter.get('equity', s['offers'][target].get('equity'))
                s['offers'][target]['status'] = 'accepted'
                s['deal_phase'] = 'closed'
        if deal_decl.get('counter_reject') and s.get('active_deal'):
            target = s['active_deal']
            if target in s['offers']:
                s['offers'][target]['status'] = 'rejected'
            s['active_deal'] = None

        # ── Check verdict ──
        verdict_match = re.search(r'VERDICT:\s*(INVEST|PASS)', resp, re.IGNORECASE)
        investor_has_offer = inv['name'] in s['offers']
        im_out = 'out' in deal_decl

        if verdict_match and not investor_has_offer and not im_out and s['ask_established']:
            verdict_match = None

        if im_out and not verdict_match:
            reason_text = strip_hedges(deal_decl.get('out', 'Stepped out.'))
            verdict_match = True
            decision = 'PASS'
            reason = reason_text

        effective_max = max_turns + 2 if (s['ask_established'] and not investor_has_offer) else max_turns

        if verdict_match or turn >= effective_max:
            if not isinstance(verdict_match, bool):
                decision = verdict_match.group(1).upper() if verdict_match else 'PASS'
                reason = strip_hedges(extract_label(resp, 'REASON') or resp.split('\n')[0])
                reason = re.sub(r'SCORE:\s*(?:STRONG|SOLID|VAGUE|EVASIVE|DODGE)\s*', '', reason, flags=re.IGNORECASE).strip()
            elif not im_out:
                decision = 'PASS'
                reason = strip_hedges(extract_label(resp, 'REASON') or resp.split('\n')[0])
                reason = re.sub(r'SCORE:\s*(?:STRONG|SOLID|VAGUE|EVASIVE|DODGE)\s*', '', reason, flags=re.IGNORECASE).strip()

            return await self._deliver_verdict(events, ri, inv, disp, decision, reason, resp)

        # ── Continue conversation ──
        q = extract_label(resp, 'QUESTION')
        if not q:
            q = re.sub(r'^(VERDICT|REASON|FOLLOW-UP):\s*', '', resp, flags=re.IGNORECASE).strip().split('\n')[0]
        q = re.sub(r'SCORE:\s*(?:STRONG|SOLID|VAGUE|EVASIVE|DODGE)\s*', '', q, flags=re.IGNORECASE).strip()
        display = clean_response(q if q else resp)

        # Turn only counts when a business answer earns another question — matches notebook
        s['turns'][ri] += 1
        s['total_turns'] += 1

        s['messages'].append({'role': 'assistant', 'content': display})
        expr = get_expression(disp)
        events.append(ev('character', key=_inv_key(inv['name']), expression=expr))
        events.append(ev('dialogue', speaker=inv['name'], text=display, accent=inv['accent']))
        events.append(ev('disposition', score=disp, name=inv['name']))
        events.append(ev('input', enabled=True, placeholder='Your answer...'))
        events.append(ev('buttons', buttons=[
            {'id': 'skip', 'label': f'Skip {inv["name"]}', 'variant': 'ghost'}
        ]))
        if s['offers']:
            events.append(ev('deal_status', ask=s['ask'], offers={
                k: {kk: vv for kk, vv in v.items() if kk != 'raw'}
                for k, v in s['offers'].items()
            }))
        return events

    # ── Verdict helpers ────────────────────────────────────────────────────

    async def _force_verdict(self, text, ri, inv, disp):
        s = self.state
        hist = s['histories']
        events = [ev('user_message', text=text), ev('thinking')]

        tone = disposition_modifier(disp, inv['name'])
        ask = s['ask']
        ask_ctx = ''
        if s['ask_established']:
            parts = []
            if ask.get('amount'): parts.append(ask['amount'])
            if ask.get('equity'): parts.append(f"for {ask['equity']}")
            if parts:
                ask_ctx = f" The founder is asking {' '.join(parts)}."

        early_prompt = (
            f"CURRENT MOOD: {tone}\n\n"
            f'The founder just answered: "{text}"\n\n'
            f"You've heard enough — make your call.{ask_ctx}\n"
            "VERDICT: INVEST or PASS\n"
            "REASON: one sentence."
        )
        msgs = hist[ri] + [
            {'role': 'user', 'content': text},
            {'role': 'user', 'content': early_prompt},
        ]
        resp = await groq_call(msgs, temperature=inv['temp'])
        dm = re.search(r'VERDICT:\s*(INVEST|PASS)', resp, re.IGNORECASE)
        decision = dm.group(1).upper() if dm else 'PASS'
        reason = strip_hedges(extract_label(resp, 'REASON') or resp.split('\n')[0])
        reason = re.sub(r'SCORE:\s*(?:STRONG|SOLID|VAGUE|EVASIVE|DODGE)\s*', '', reason, flags=re.IGNORECASE).strip()
        hist[ri] = msgs + [{'role': 'assistant', 'content': resp}]
        s['histories'] = hist

        return await self._deliver_verdict(events, ri, inv, disp, decision, reason, resp)

    async def _deliver_verdict(self, events, ri, inv, disp, decision, reason, resp, display_text=None):
        s = self.state
        hist = s['histories']

        s['verdicts'].append({
            'name': inv['name'], 'title': inv['title'],
            'decision': decision, 'reason': reason,
        })
        s['phase'] = 'verdict'
        verdict_text = display_text or f'{decision} — {reason}'
        s['messages'].append({'role': 'assistant', 'content': verdict_text})

        expr = get_expression(disp, is_verdict=True, decision=decision)
        events.append(ev('character', key=_inv_key(inv['name']), expression=expr))
        events.append(ev('sfx', sound='verdict_invest' if decision == 'INVEST' else 'verdict_pass'))
        events.append(ev('dialogue', speaker=inv['name'],
                         text=verdict_text, accent=inv['accent']))
        events.append(ev('pills', data=pills_data(ri, s['verdicts'])))
        events.append(ev('disposition', score=disp, name=inv['name']))
        events.append(ev('input', enabled=False))

        # Extract claims for subsequent investors
        if ri < 3:
            try:
                user_msgs = [m['content'] for m in hist[ri] if m.get('role') == 'user'
                             and not any(m['content'].startswith(p) for p in
                             ['Startup pitch', 'Continue', 'The founder',
                              'You have asked', 'CURRENT MOOD', 'Deliver'])]
                inv_qs = [extract_label(m['content'], 'QUESTION') for m in hist[ri]
                          if m.get('role') == 'assistant']
                inv_qs = [q for q in inv_qs if q]
                q1 = inv_qs[0] if inv_qs else ''
                q2 = inv_qs[1] if len(inv_qs) > 1 else ''
                a1 = user_msgs[0] if user_msgs else ''
                a2 = user_msgs[1] if len(user_msgs) > 1 else ''
                if a1 or a2:
                    claims = await extract_claims(inv['name'], q1, a1, q2, a2)
                    if claims:
                        s['claims'].append(claims)
            except Exception:
                pass

        btns = [{'id': 'continue_talk', 'label': '💬 Keep Talking', 'variant': 'ghost'}]
        if ri >= 3:
            btns.append({'id': 'continue', 'label': 'See Scorecard →', 'variant': 'primary'})
        else:
            nxt = INVESTORS[ri + 1]
            btns.append({'id': 'continue', 'label': f'Next: {nxt["name"]} →', 'variant': 'primary'})
        events.append(ev('buttons', buttons=btns))

        if s['offers']:
            events.append(ev('deal_status', ask=s['ask'], offers={
                k: {kk: vv for kk, vv in v.items() if kk != 'raw'}
                for k, v in s['offers'].items()
            }))
        return events

    # ── Post-verdict conversation ──────────────────────────────────────────

    async def _handle_post_verdict(self, text):
        s = self.state
        ri = s['round']
        inv = INVESTORS[min(ri, 3)]
        hist = s['histories']
        events = [ev('user_message', text=text), ev('thinking')]

        framing = (
            "You have already made your investment decision. The formal pitch is over. "
            "Respond to the founder as yourself — be warm, direct, and human."
        )
        msgs = hist[ri] + [
            {'role': 'user', 'content': framing},
            {'role': 'user', 'content': text},
        ]
        try:
            resp = await groq_call(msgs, temperature=inv['temp'], max_tokens=280)
            s['messages'].append({'role': 'assistant', 'content': resp})
            hist[ri] = msgs + [{'role': 'assistant', 'content': resp}]
            s['histories'] = hist
        except Exception as e:
            resp = f'Something went wrong ({e}).'
            s['messages'].append({'role': 'assistant', 'content': resp})

        display = clean_response(resp)
        events.append(ev('dialogue', speaker=inv['name'], text=display, accent=inv['accent']))
        events.append(ev('input', enabled=True, placeholder='Ask them anything...'))

        nxt = INVESTORS[ri + 1] if ri < 3 else None
        btns = [{'id': 'continue_talk', 'label': '💬 Keep Talking', 'variant': 'ghost'}]
        if nxt:
            btns.append({'id': 'continue', 'label': f'Next: {nxt["name"]} →', 'variant': 'primary'})
        else:
            btns.append({'id': 'continue', 'label': 'See Scorecard →', 'variant': 'primary'})
        events.append(ev('buttons', buttons=btns))
        return events

    # ── Navigation ─────────────────────────────────────────────────────────

    async def _skip(self):
        s = self.state
        if s['phase'] != 'questioning':
            return []

        ri = s['round']
        inv = INVESTORS[min(ri, 3)]
        hist = s['histories']
        events = [ev('thinking')]

        diff_ctx = build_difficulty_context(s)
        base_msgs = hist[ri] if hist[ri] else [
            {'role': 'system', 'content': (PERSONALITY_PREFIX + '\n\n' + inv['system'] + diff_ctx).strip()}
        ]
        msgs = base_msgs + [{'role': 'user', 'content': SKIP_EXITS[inv['name']]}]

        try:
            parting = await groq_call(msgs, temperature=inv['temp'], max_tokens=120)
            parting = clean_response(parting)
        except Exception:
            parting = '...'

        s['messages'].append({'role': 'assistant', 'content': parting})
        s['verdicts'].append({
            'name': inv['name'], 'title': inv['title'],
            'decision': 'PASS', 'reason': 'Skipped.',
        })
        s['phase'] = 'verdict'

        events.append(ev('character', key=_inv_key(inv['name']), expression='skeptical'))
        events.append(ev('sfx', sound='verdict_pass'))
        events.append(ev('dialogue', speaker=inv['name'], text=parting, accent=inv['accent']))
        events.append(ev('pills', data=pills_data(ri, s['verdicts'])))
        events.append(ev('input', enabled=False))

        btns = []
        if ri >= 3:
            btns.append({'id': 'continue', 'label': 'See Scorecard →', 'variant': 'primary'})
        else:
            nxt = INVESTORS[ri + 1]
            btns.append({'id': 'continue', 'label': f'Next: {nxt["name"]} →', 'variant': 'primary'})
        events.append(ev('buttons', buttons=btns))
        return events

    async def _continue(self):
        s = self.state
        ri = s['round']
        s['post_verdict_mode'] = False

        if ri >= 3:
            # Scorecard
            s['phase'] = 'done'
            scorecard_data = []
            for v in s['verdicts']:
                sc = {'name': v['name'], 'title': v.get('title', ''),
                      'decision': v['decision'], 'reason': v['reason']}
                offer = s['offers'].get(v['name'])
                if offer and offer.get('status') == 'accepted':
                    sc['deal'] = f"{offer.get('amount', '?')} for {offer.get('equity', '?')}"
                scorecard_data.append(sc)
            return [
                ev('scene', screen='scorecard'),
                ev('scorecard', verdicts=scorecard_data, total=len(s['verdicts'])),
                ev('music', track='menu'),
            ]

        # Advance to next investor
        next_ri = ri + 1
        next_inv = INVESTORS[next_ri]
        last_v = s['verdicts'][-1] if s['verdicts'] else None
        events = []

        events.append(ev('scene', screen='transition'))
        events.append(ev('transition',
                         outgoing=inv_info(INVESTORS[ri]),
                         verdict=last_v['decision'] if last_v else 'PASS',
                         reason=last_v['reason'] if last_v else '',
                         incoming=inv_info(next_inv)))

        return events

    async def _advance_to_investor(self):
        """Called after transition screen — start next investor's round."""
        s = self.state
        if s['round'] >= len(INVESTORS) - 1:
            s['phase'] = 'done'
            scorecard_data = []
            for v in s['verdicts']:
                sc = {'name': v['name'], 'title': v.get('title', ''),
                      'decision': v['decision'], 'reason': v['reason']}
                offer = s['offers'].get(v['name'])
                if offer and offer.get('status') == 'accepted':
                    sc['deal'] = f"{offer.get('amount', '?')} for {offer.get('equity', '?')}"
                scorecard_data.append(sc)
            return [
                ev('scene', screen='scorecard'),
                ev('scorecard', verdicts=scorecard_data, total=len(s['verdicts'])),
                ev('music', track='menu'),
            ]
        ri = s['round'] + 1
        s['round'] = ri
        s['phase'] = 'questioning'
        s['messages'] = []
        s['post_verdict_mode'] = False

        inv = INVESTORS[ri]
        events = []

        prior = ''
        if s['claims']:
            prior = (
                '\n\nWhat this founder has already told other investors '
                '(treat any contradictions as fair game):\n\n'
                + '\n\n'.join(s['claims'])
            )
        diff_ctx = build_difficulty_context(s)
        msgs = [
            {'role': 'system', 'content': (PERSONALITY_PREFIX + '\n\n' + inv['system'] + prior + diff_ctx).strip()},
            {'role': 'user', 'content': (
                f"Startup pitch:\n\n{s['pitch']}\n\n"
                "Ask your first question.\n\n"
                f"{QUESTION_STYLE_GUIDANCE}"
            )},
        ]

        events.append(ev('scene', screen='game'))
        events.append(ev('pills', data=pills_data(ri, s['verdicts'])))
        events.append(ev('investor', **inv_info(inv)))
        events.append(ev('character', key=_inv_key(inv['name']), expression='neutral'))
        events.append(ev('thinking'))

        resp = await groq_call(msgs, temperature=inv['temp'], max_tokens=320)
        q = extract_label(resp, 'QUESTION')
        s['histories'][ri] = msgs + [{'role': 'assistant', 'content': resp}]
        s['turns'][ri] = 1
        s['messages'].append({'role': 'assistant', 'content': q})

        expr = get_expression(s['dispositions'][ri])
        events.append(ev('character', key=_inv_key(inv['name']), expression=expr))
        events.append(ev('dialogue', speaker=inv['name'], text=q, accent=inv['accent']))
        events.append(ev('disposition', score=s['dispositions'][ri], name=inv['name']))
        events.append(ev('input', enabled=True, placeholder='Your answer...'))
        events.append(ev('buttons', buttons=[
            {'id': 'skip', 'label': f'Skip {inv["name"]}', 'variant': 'ghost'}
        ]))
        return events

    async def _replay(self):
        """Replay = new game from mode select."""
        return self._start()

    async def _go_anyway(self):
        text = self.state.get('pending_pitch', self.state.get('pitch', ''))
        return await self._enter_questioning(text)

    def _pitch_again(self):
        self.state['phase'] = 'setup'
        return [
            ev('scene', screen='pitch'),
            ev('input', enabled=True, placeholder='Edit your pitch...',
               value=self.state.get('pending_pitch', '')),
        ]

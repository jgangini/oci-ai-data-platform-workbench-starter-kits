import { createVoiceSession } from '../vendor/gods-eye-view/src/voice/session.js';

// GodEye owns session lifetime; this adapter speaks the TERRITORIAL JSON contract.
// ponytail: final-response JSON only; add streaming when AIDP's wire contract is verified.
export function createTerritorialSession({ request, context, onReply, onError, onBusy, onSubmitted = () => {} }) {
  let pending;
  let sessionId;
  let submitted = 0;
  onSubmitted(submitted);
  return createVoiceSession({
    runner: async () => { throw new Error('Confirm map actions in the dashboard.'); },
    createAdapter: ({ emit, signal }) => ({
      start() { emit({ type: 'state', state: 'ready' }); },
      stop() { pending?.abort(); pending = undefined; onBusy(false); },
      sendMapEvent() {},
      async sendText(question) {
        if (typeof question !== 'string' || !question.trim()) return;
        pending?.abort();
        const turn = new AbortController();
        pending = turn;
        const selected = context();
        onSubmitted(++submitted);
        onBusy(true);
        try {
          const reply = await request('/api/territorial/chat', {
            method: 'POST',
            signal: AbortSignal.any([signal, turn.signal]),
            body: JSON.stringify({ question, session_id: sessionId, ...selected }),
          });
          if (turn.signal.aborted || pending !== turn) return;
          if (typeof reply.answer !== 'string' || !reply.answer.trim() || reply.version !== selected.version) {
            throw new Error('The agent did not return a valid answer for this publication.');
          }
          sessionId = reply.session_id;
          onReply(reply, question);
          emit({ type: 'completion' });
        } catch (error) {
          if (!turn.signal.aborted && !signal.aborted && pending === turn) onError(error);
        } finally {
          if (pending === turn) { pending = undefined; onBusy(false); }
        }
      },
    }),
  });
}

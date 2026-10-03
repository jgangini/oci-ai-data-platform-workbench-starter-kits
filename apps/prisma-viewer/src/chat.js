import { createVoiceSession } from '../vendor/gods-eye-view/src/voice/session.js';

// GodEye owns session lifetime; this adapter speaks the PRISMA JSON contract.
// ponytail: final-response JSON only; add streaming when AIDP's wire contract is verified.
export function createPrismaSession({ request, context, onReply, onError, onBusy }) {
  let pending;
  let sessionId;
  return createVoiceSession({
    runner: async () => { throw new Error('Las acciones requieren confirmación en el tablero.'); },
    createAdapter: ({ emit, signal }) => ({
      start() { emit({ type: 'state', state: 'ready' }); },
      stop() { pending?.abort(); pending = undefined; onBusy(false); },
      sendMapEvent() {},
      async sendText(question) {
        pending?.abort();
        const turn = new AbortController();
        pending = turn;
        const selected = context();
        onBusy(true);
        try {
          const reply = await request('/api/prisma/chat', {
            method: 'POST',
            signal: AbortSignal.any([signal, turn.signal]),
            body: JSON.stringify({ question, session_id: sessionId, ...selected }),
          });
          if (turn.signal.aborted || pending !== turn) return;
          if (typeof reply.answer !== 'string' || !reply.answer.trim() || reply.version !== selected.version) {
            throw new Error('El agente no devolvió una respuesta válida para esta publicación.');
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

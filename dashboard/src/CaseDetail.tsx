import { useEffect, useState } from 'react';
import { ArrowLeft, Loader2, Mic, AlertCircle } from 'lucide-react';
import { statusBadge, statusLabel } from './App';

/**
 * One case: who, what they paid, whether a lawyer has it, and the conversation.
 *
 * The conversation is read by the product backend from the AI service with
 * this operator's own token. If that fails the rest of the case still shows,
 * with the reason — a slow AI service must not blank the page a lawyer needs.
 */

interface CaseUser {
  id: string;
  email: string | null;
  display_name: string | null;
  phone: string | null;
  joined_at: string;
  wp_user_id: number | null;
  wp_role: string | null;
}

interface CaseMessage {
  seq: number;
  role: 'user' | 'assistant';
  content: string;
  source_label: string | null;
  input_mode: 'text' | 'voice';
  status: string;
  created_at: string | null;
}

interface Conversation {
  session_id: string;
  status: string;
  lang: string | null;
  title: string | null;
  created_at: string;
  messages: CaseMessage[];
  voice_messages: number;
  messages_used: number;
  message_limit: number;
}

interface CaseRecord {
  consultation_id: string;
  status: string;
  service_id: string;
  service_slug: string | null;
  service_name: string;
  needs_conversation: boolean;
  client_notes: string | null;
  amount_cents: number;
  currency: string;
  ai_session_id: string | null;
  created_at: string;
  paid_at: string | null;
  escalated: boolean;
  escalation_error: string | null;
  chat_language: string | null;
  escalation_summary: string | null;
  user: CaseUser | null;
  latest_payment: {
    provider_txn_id: string;
    payment_status: string;
    payment_amount_cents: number;
  } | null;
  conversation: Conversation | null;
  conversation_error: string | null;
}

// What the model's provenance label means to someone reading a case, not to
// someone debugging retrieval.
const LABELS: Record<string, string> = {
  documents: 'From the documents',
  mixed: 'Partly general knowledge',
  model_knowledge: 'General knowledge only',
  refused: 'Declined',
  escalate: 'Asked to book',
};

interface Props {
  consultationId: string;
  backend: string;
  authHeaders: () => Promise<Record<string, string>>;
  onBack: () => void;
  /** The list behind this page should refresh after a status change. */
  onChanged?: () => void;
  formatMoney: (cents: number, currency?: string) => string;
  formatDate: (value: string) => string;
}

/** Which statuses a lawyer may move an order to, from where it is. */
const NEXT: Record<string, Array<{ to: string; label: string }>> = {
  pending: [{ to: 'cancelled', label: 'Cancel order' }],
  paid: [{ to: 'in_progress', label: 'Start work' }, { to: 'completed', label: 'Mark completed' }],
  in_progress: [{ to: 'completed', label: 'Mark completed' }],
  completed: [{ to: 'in_progress', label: 'Reopen' }],
};

async function readEnvelope(res: Response): Promise<any> {
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new Error(body?.error?.message || `HTTP ${res.status}`);
  }
  return body;
}

export default function CaseDetail({
  consultationId, backend, authHeaders, onBack, onChanged, formatMoney, formatDate,
}: Props) {
  const [record, setRecord] = useState<CaseRecord | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [changing, setChanging] = useState(false);

  const setStatus = async (to: string) => {
    if (!record) return;
    if (to === 'cancelled' && !window.confirm('Cancel this unpaid order?')) return;
    setChanging(true);
    setError('');
    try {
      const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
      const updated = await fetch(
        `${backend}/admin/billing/consultations/${encodeURIComponent(record.consultation_id)}/status`,
        { method: 'PATCH', headers, body: JSON.stringify({ status: to }) },
      ).then(readEnvelope);
      // The status route returns the order without the conversation; keep
      // what we already have of it.
      setRecord({ ...record, ...updated });
      onChanged?.();
    } catch (err: any) {
      setError(err.message || 'Could not change the status.');
    } finally {
      setChanging(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError('');
      try {
        const headers = await authHeaders();
        const data = await fetch(
          `${backend}/admin/billing/consultations/${encodeURIComponent(consultationId)}`,
          { headers },
        ).then(readEnvelope);
        if (!cancelled) setRecord(data);
      } catch (err: any) {
        if (!cancelled) setError(err.message || 'Could not load this case.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [consultationId]);

  const handover = (r: CaseRecord) => {
    if (r.status === 'pending' || r.status === 'cancelled' || r.status === 'refunded') {
      return <span className="badge badge-neutral">Not paid</span>;
    }
    if (r.escalated) {
      return <span className="badge badge-success">{r.needs_conversation ? 'With a lawyer' : 'Received'}</span>;
    }
    if (!r.ai_session_id || !r.needs_conversation) return <span className="badge badge-neutral">Nothing to hand over</span>;
    return (
      <span className="badge badge-danger" title={r.escalation_error || ''}>
        Not handed over
      </span>
    );
  };

  return (
    <div className="case-page animate-fade-in">
      <button className="back-btn" onClick={onBack}>
        <ArrowLeft size={16} /> All orders
      </button>

      {loading && (
        <div className="loader-container"><Loader2 size={32} className="animate-spin" /></div>
      )}

      {!loading && error && (
        <div className="error-message"><AlertCircle size={16} /> {error}</div>
      )}

      {!loading && record && (
        <>
          <div className="page-header">
            <h1 className="page-title">{record.user?.display_name || 'Client'}</h1>
            <p className="page-subtitle">
              <span dir="rtl">{record.service_name}</span> · Case {record.consultation_id} · opened {formatDate(record.created_at)}
            </p>
          </div>

          {(NEXT[record.status] ?? []).length > 0 && (
            <div className="status-actions">
              <span className={`badge ${statusBadge(record.status)}`}>{statusLabel(record.status)}</span>
              {(NEXT[record.status] ?? []).map((n) => (
                <button key={n.to} className="open-btn" disabled={changing} onClick={() => setStatus(n.to)}>
                  {changing ? <Loader2 size={13} className="animate-spin" /> : n.label}
                </button>
              ))}
            </div>
          )}

          <div className="case-grid">
            <section className="case-card">
              <h2 className="case-card-title">Client</h2>
              <dl className="facts">
                <dt>Email</dt><dd>{record.user?.email || '—'}</dd>
                {/* Phone is not collected yet; the row is here so the page
                    does not change shape when it is. */}
                <dt>Phone</dt><dd className="ltr">{record.user?.phone || 'Not provided'}</dd>
                <dt>WordPress</dt>
                <dd>
                  {record.user?.wp_user_id != null
                    ? `#${record.user.wp_user_id}${record.user.wp_role ? ` · ${record.user.wp_role}` : ''}`
                    : 'Not linked'}
                </dd>
                <dt>Joined</dt><dd>{record.user ? formatDate(record.user.joined_at) : '—'}</dd>
              </dl>
            </section>

            <section className="case-card">
              <h2 className="case-card-title">Payment</h2>
              <dl className="facts">
                <dt>Amount</dt><dd className="amount-cell">{formatMoney(record.amount_cents, record.currency)}</dd>
                <dt>Service</dt><dd dir="rtl">{record.service_name}</dd>
                <dt>Status</dt>
                <dd>
                  <span className={`badge ${statusBadge(record.status)}`}>
                    {statusLabel(record.status)}
                  </span>
                </dd>
                <dt>Paid</dt><dd>{record.paid_at ? formatDate(record.paid_at) : '—'}</dd>
                <dt>Paymob</dt><dd className="ltr">{record.latest_payment?.provider_txn_id || '—'}</dd>
                <dt>Handover</dt><dd>{handover(record)}</dd>
              </dl>
            </section>

            <section className="case-card">
              <h2 className="case-card-title">Conversation</h2>
              <dl className="facts">
                <dt>Language</dt><dd>{record.chat_language || record.conversation?.lang || '—'}</dd>
                <dt>Messages</dt>
                <dd>
                  {record.conversation
                    ? `${record.conversation.messages_used} of ${record.conversation.message_limit} used`
                    : '—'}
                </dd>
                <dt>Voice</dt>
                <dd>
                  {record.conversation
                    ? `${record.conversation.voice_messages} spoken`
                    : '—'}
                </dd>
              </dl>
            </section>
          </div>

          {record.client_notes && (
            <section className="case-card case-summary-card">
              <h2 className="case-card-title">The client's description</h2>
              <p className="case-summary-text" dir="auto">{record.client_notes}</p>
            </section>
          )}

          {record.escalation_summary && (
            <section className="case-card case-summary-card">
              <h2 className="case-card-title">Summary for the lawyer</h2>
              <p className="case-summary-text" dir="auto">{record.escalation_summary}</p>
            </section>
          )}

          <section className="case-card">
            <h2 className="case-card-title">Transcript</h2>

            {!record.ai_session_id && (
              <p className="muted">This order was placed without a conversation.</p>
            )}

            {record.conversation_error && (
              <div className="error-message">
                <AlertCircle size={16} /> Transcript unavailable: {record.conversation_error}
              </div>
            )}

            {record.conversation && (
              <ol className="transcript" dir="rtl">
                {record.conversation.messages.map((m) => (
                  <li key={m.seq} className={`turn turn-${m.role}`}>
                    <div className="turn-meta">
                      <span>{m.role === 'user' ? 'Client' : 'Assistant'}</span>
                      {m.input_mode === 'voice' && (
                        <span className="badge badge-language" title="Spoken, then transcribed">
                          <Mic size={11} /> Voice
                        </span>
                      )}
                      {m.role === 'assistant' && m.source_label && (
                        <span className="badge badge-neutral">
                          {LABELS[m.source_label] || m.source_label}
                        </span>
                      )}
                      {m.status !== 'complete' && (
                        <span className="badge badge-danger">{m.status}</span>
                      )}
                      {m.created_at && <span className="muted">{formatDate(m.created_at)}</span>}
                    </div>
                    <div className="turn-text" dir="auto">{m.content || '—'}</div>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </>
      )}
    </div>
  );
}

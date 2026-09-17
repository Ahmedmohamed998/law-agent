import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Loader2, AlertCircle, Plus, Pencil, Check, X } from 'lucide-react';

/**
 * The catalogue: what the site sells and what the assistant may suggest.
 *
 * Edited here and nowhere else. A price saved on this page is the price the
 * next order is charged; the site only displays it. There is no delete —
 * orders reference services — so "remove" means deactivate.
 */

export interface Service {
  service_id: string;
  slug: string;
  name: string;
  description: string;
  price_cents: number;
  currency: string;
  active: boolean;
  sort_order: number;
  needs_conversation: boolean;
  needs_notes: boolean;
  ai_hint: string;
  suggestable: boolean;
  updated_at: string;
}

interface Draft {
  slug: string;
  name: string;
  description: string;
  price: string; // in riyals, as typed; converted on save
  currency: string;
  active: boolean;
  sort_order: string;
  needs_conversation: boolean;
  needs_notes: boolean;
  ai_hint: string;
  suggestable: boolean;
}

const EMPTY: Draft = {
  slug: '', name: '', description: '', price: '', currency: 'SAR', active: true,
  sort_order: '100', needs_conversation: false, needs_notes: false, ai_hint: '', suggestable: true,
};

function toDraft(s: Service): Draft {
  return {
    slug: s.slug,
    name: s.name,
    description: s.description,
    price: (s.price_cents / 100).toString(),
    currency: s.currency,
    active: s.active,
    sort_order: String(s.sort_order),
    needs_conversation: s.needs_conversation,
    needs_notes: s.needs_notes,
    ai_hint: s.ai_hint,
    suggestable: s.suggestable,
  };
}

interface Props {
  backend: string;
  authHeaders: () => Promise<Record<string, string>>;
  formatMoney: (cents: number, currency?: string) => string;
}

async function readEnvelope(res: Response): Promise<any> {
  const text = await res.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!res.ok) throw new Error(body?.error?.message || `HTTP ${res.status}`);
  return body;
}

export default function Services({ backend, authHeaders, formatMoney }: Props) {
  const [services, setServices] = useState<Service[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // null: nothing open; 'new': creating; otherwise the id being edited.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const headers = await authHeaders();
      const data = await fetch(`${backend}/admin/billing/services`, { headers }).then(readEnvelope);
      setServices(data);
    } catch (err: any) {
      setError(err.message || 'Could not load services.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const startNew = () => { setDraft(EMPTY); setEditing('new'); setSaveError(''); };
  const startEdit = (s: Service) => { setDraft(toDraft(s)); setEditing(s.service_id); setSaveError(''); };
  const cancel = () => { setEditing(null); setSaveError(''); };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError('');
    try {
      // Whole riyals or halalas typed as a decimal; stored as an integer.
      const price = Math.round(parseFloat(draft.price.replace(',', '.')) * 100);
      if (!Number.isFinite(price) || price <= 0) throw new Error('Enter a price greater than zero.');

      const body: Record<string, unknown> = {
        name: draft.name.trim(),
        description: draft.description.trim(),
        price_cents: price,
        currency: draft.currency.trim().toUpperCase() || 'SAR',
        active: draft.active,
        sort_order: parseInt(draft.sort_order, 10) || 100,
        needs_conversation: draft.needs_conversation,
        needs_notes: draft.needs_notes,
        ai_hint: draft.ai_hint.trim(),
        suggestable: draft.suggestable,
      };
      // The slug is set once; the backend refuses to change it afterwards.
      if (editing === 'new') body.slug = draft.slug.trim();

      const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
      const url = editing === 'new'
        ? `${backend}/admin/billing/services`
        : `${backend}/admin/billing/services/${encodeURIComponent(editing as string)}`;
      await fetch(url, { method: editing === 'new' ? 'POST' : 'PATCH', headers, body: JSON.stringify(body) })
        .then(readEnvelope);
      setEditing(null);
      await load();
    } catch (err: any) {
      setSaveError(err.message || 'Could not save.');
    } finally {
      setSaving(false);
    }
  };

  const toggleActive = async (s: Service) => {
    try {
      const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
      await fetch(`${backend}/admin/billing/services/${encodeURIComponent(s.service_id)}`, {
        method: 'PATCH', headers, body: JSON.stringify({ active: !s.active }),
      }).then(readEnvelope);
      await load();
    } catch (err: any) {
      setError(err.message || 'Could not update.');
    }
  };

  const field = (key: keyof Draft) => (e: { target: { value: string } }) =>
    setDraft({ ...draft, [key]: e.target.value });
  const flag = (key: keyof Draft) => (e: { target: { checked: boolean } }) =>
    setDraft({ ...draft, [key]: e.target.checked });

  return (
    <div className="animate-fade-in">
      <div className="page-header services-header">
        <div>
          <h1 className="page-title">Services</h1>
          <p className="page-subtitle">What the site sells. Prices here are the prices charged.</p>
        </div>
        {editing === null && (
          <button className="open-btn" onClick={startNew}>
            <Plus size={14} /> New service
          </button>
        )}
      </div>

      {error && <div className="error-message"><AlertCircle size={16} /> {error}</div>}

      {editing !== null && (
        <form className="case-card service-form" onSubmit={save}>
          <h2 className="case-card-title">{editing === 'new' ? 'New service' : 'Edit service'}</h2>

          <div className="service-form-grid">
            <label className="input-group">
              <span className="input-label">Slug (URL name, letters-digits-hyphens)</span>
              <input className="input-field ltr" value={draft.slug} onChange={field('slug')}
                     placeholder="contract-review" required disabled={editing !== 'new'}
                     pattern="[a-z0-9]+(-[a-z0-9]+)*" />
            </label>
            <label className="input-group">
              <span className="input-label">Name (Arabic)</span>
              <input className="input-field" dir="rtl" value={draft.name} onChange={field('name')}
                     placeholder="مراجعة عقد" required />
            </label>
            <label className="input-group">
              <span className="input-label">Price ({draft.currency || 'SAR'})</span>
              <input className="input-field ltr" inputMode="decimal" value={draft.price} onChange={field('price')}
                     placeholder="300" required />
            </label>
            <label className="input-group">
              <span className="input-label">Display order (lower first)</span>
              <input className="input-field ltr" inputMode="numeric" value={draft.sort_order} onChange={field('sort_order')} />
            </label>
          </div>

          <label className="input-group">
            <span className="input-label">Description (shown to clients)</span>
            <textarea className="input-field" dir="rtl" rows={3} value={draft.description} onChange={field('description')}
                      placeholder="يراجع المحامي العقد ويوضح لك البنود التي تحتاج انتباهًا قبل التوقيع." />
          </label>

          <label className="input-group">
            <span className="input-label">
              When the assistant should suggest this (Arabic, one sentence). Empty = never suggested.
            </span>
            <textarea className="input-field" dir="rtl" rows={2} value={draft.ai_hint} onChange={field('ai_hint')}
                      placeholder="عندما يريد العميل فهم أو تعديل عقد قبل توقيعه." />
          </label>

          <div className="service-flags">
            <label><input type="checkbox" checked={draft.active} onChange={flag('active')} /> Offered on the site</label>
            <label><input type="checkbox" checked={draft.needs_conversation} onChange={flag('needs_conversation')} />
              The lawyer needs the chat conversation (hands over the transcript, locks the chat)</label>
            <label><input type="checkbox" checked={draft.needs_notes} onChange={flag('needs_notes')} />
              The client must describe the case before paying</label>
            <label><input type="checkbox" checked={draft.suggestable} onChange={flag('suggestable')} />
              The assistant may suggest it</label>
          </div>

          {saveError && <div className="error-message"><AlertCircle size={16} /> {saveError}</div>}

          <div className="service-form-actions">
            <button type="submit" className="submit-btn" disabled={saving}>
              {saving ? <Loader2 className="animate-spin" size={16} /> : <><Check size={14} /> Save</>}
            </button>
            <button type="button" className="logout-btn" onClick={cancel} disabled={saving}>
              <X size={14} /> Cancel
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="loader-container"><Loader2 size={32} className="animate-spin" /></div>
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Service</th>
                <th>Price</th>
                <th>Needs</th>
                <th>Assistant</th>
                <th>Status</th>
                <th><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {services.map((s) => (
                <tr key={s.service_id} className={s.active ? '' : 'row-inactive'}>
                  <td>
                    <div className="user-cell">
                      <span className="user-name" dir="rtl">{s.name}</span>
                      <span className="user-email ltr">{s.slug}</span>
                      {s.description && <span className="case-summary" dir="rtl">{s.description}</span>}
                    </div>
                  </td>
                  <td className="amount-cell">{formatMoney(s.price_cents, s.currency)}</td>
                  <td>
                    {s.needs_conversation && <span className="badge badge-language">Conversation</span>}{' '}
                    {s.needs_notes && <span className="badge badge-language">Notes</span>}
                    {!s.needs_conversation && !s.needs_notes && <span className="badge badge-neutral">—</span>}
                  </td>
                  <td>
                    {s.suggestable && s.ai_hint
                      ? <span className="badge badge-success" title={s.ai_hint}>May suggest</span>
                      : <span className="badge badge-neutral">Never</span>}
                  </td>
                  <td>
                    <span className={`badge ${s.active ? 'badge-success' : 'badge-neutral'}`}>
                      {s.active ? 'ACTIVE' : 'INACTIVE'}
                    </span>
                  </td>
                  <td className="service-actions">
                    <button className="open-btn" onClick={() => startEdit(s)}><Pencil size={13} /> Edit</button>
                    <button className="open-btn" onClick={() => toggleActive(s)}>
                      {s.active ? 'Deactivate' : 'Activate'}
                    </button>
                  </td>
                </tr>
              ))}
              {services.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>No services yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

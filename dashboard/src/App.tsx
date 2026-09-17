import { useState, useEffect } from 'react';
import type { FormEvent } from 'react';
import { 
  ShieldCheck, 
  LogOut, 
  Users, 
  CreditCard, 
  Activity, 
  AlertCircle,
  Loader2
} from 'lucide-react';
import './App.css';
import CaseDetail from './CaseDetail';
import Services from './Services';

// --- Types ---
interface DashboardStats {
  total_consultations: number;
  paid_consultations: number;
  pending_consultations: number;
  failed_payments: number;
  total_revenue_cents: number;
  by_service: Array<{
    service_id: string;
    service_name: string;
    orders: number;
    paid: number;
    revenue_cents: number;
  }>;
}

/** One order's status, as a badge class. Paid and its two work states are all "good". */
export function statusBadge(status: string): string {
  if (status === 'paid' || status === 'in_progress' || status === 'completed') return 'badge-success';
  if (status === 'pending') return 'badge-warning';
  return 'badge-danger';
}

export function statusLabel(status: string): string {
  return status.replace('_', ' ').toUpperCase();
}

interface User {
  id: string;
  email: string | null;
  display_name: string | null;
  phone: string | null;
  joined_at: string;
  /** WordPress owns identity; null for anyone who predates the link. */
  wp_user_id: number | null;
  wp_role: string | null;
}

interface Payment {
  provider_txn_id: string;
  payment_status: string;
  payment_amount_cents: number;
}

interface Consultation {
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
  user: User | null;
  latest_payment: Payment | null;
  chat_language: string | null;
  escalation_summary: string | null;
}

const BACKEND = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8001';
const API_BASE = `${BACKEND}/admin/billing`;

/**
 * This dashboard signs in as a person.
 *
 * It used to authenticate with ADMIN_API_KEY, typed into a password box and
 * kept in localStorage. That key is the service-to-service credential — the
 * same one that erases users and escalates sessions — so every dashboard user
 * held a copy of it, any XSS on this origin exfiltrated it, and it could
 * neither be revoked for one person nor attributed to anyone.
 *
 * It is now an ordinary login against the product backend, and the endpoints
 * require an `owner` or `admin` role in the token. The access token lives
 * about fifteen minutes; the refresh token is the revocable half.
 */
const SESSION_KEY = 'lawAgentAdminSession';

interface Session {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
  email: string | null;
}

function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

function saveSession(session: Session | null) {
  try {
    if (session) localStorage.setItem(SESSION_KEY, JSON.stringify(session));
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    /* a blocked store just means signing in again next visit */
  }
}

function sessionFrom(payload: any): Session {
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    // 30s of slack, so a token never expires mid-request.
    expiresAt: Date.now() + Math.max(0, (payload.expires_in ?? 900) - 30) * 1000,
    email: payload.user?.email ?? null,
  };
}

/** Every status from this backend carries { error: { code, message } }. */
async function envelope(res: Response): Promise<any> {
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const err = body?.error ?? {};
    throw Object.assign(new Error(err.message || res.statusText), {
      code: err.code,
      status: res.status,
    });
  }
  return body;
}

function App() {
  const [email, setEmail] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [session, setSession] = useState<Session | null>(loadSession());
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [authError, setAuthError] = useState<string>('');

  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [consultations, setConsultations] = useState<Consultation[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  // The case being viewed, or null for the overview.
  const [selectedCase, setSelectedCase] = useState<string | null>(null);
  const [page, setPage] = useState<'orders' | 'services'>('orders');
  // Filters on the orders table. Empty means all.
  const [serviceFilter, setServiceFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');

  // Resume a stored session on load. A dead one just shows the login form.
  useEffect(() => {
    const stored = loadSession();
    if (stored) {
      loadData(stored).catch(() => clearSession());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const clearSession = () => {
    setSession(null);
    saveSession(null);
    setIsAuthenticated(false);
    setStats(null);
    setConsultations([]);
    setPassword('');
  };

  /** A session with a live access token, refreshed if it has aged out. */
  const fresh = async (current: Session): Promise<Session> => {
    if (Date.now() < current.expiresAt) return current;

    const res = await fetch(`${BACKEND}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: current.refreshToken }),
    });
    const next = sessionFrom(await envelope(res));
    setSession(next);
    saveSession(next);
    return next;
  };

  const loadData = async (current: Session) => {
    setIsLoading(true);
    setAuthError('');
    try {
      const live = await fresh(current);
      const headers = { Authorization: `Bearer ${live.accessToken}` };

      const statsData = await fetch(`${API_BASE}/stats`, { headers }).then(envelope);
      const consultsData = await fetch(`${API_BASE}/consultations`, { headers }).then(envelope);

      setStats(statsData);
      setConsultations(consultsData);
      setSession(live);
      setIsAuthenticated(true);
      saveSession(live);
    } catch (err: any) {
      // A 403 here is a real account without the role, which is a different
      // problem from a wrong password and deserves to say so.
      setAuthError(
        err.status === 403
          ? 'This account does not have an admin or owner role.'
          : err.status === 401
            ? 'Session expired. Sign in again.'
            : err.message || 'Connection failed',
      );
      setIsAuthenticated(false);
      throw err;
    } finally {
      setIsLoading(false);
    }
  };

  const handleLogin = async (e: FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setAuthError('');
    try {
      const res = await fetch(`${BACKEND}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      const next = sessionFrom(await envelope(res));
      setSession(next);
      saveSession(next);
      setPassword('');
      await loadData(next);
    } catch (err: any) {
      if (err.status === 401) setAuthError('Wrong email or password.');
      else if (!err.status) setAuthError(err.message || 'Connection failed');
      setIsAuthenticated(false);
    } finally {
      setIsLoading(false);
    }
  };

  const handleLogout = () => {
    // Revoking the refresh token is what actually ends the session; clearing
    // local state alone would leave a valid credential live in the backend.
    if (session) {
      fetch(`${BACKEND}/auth/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: session.refreshToken }),
      }).catch(() => undefined);
    }
    clearSession();
  };

  /** Headers for an API call, refreshing the access token if it has aged out. */
  const authHeaders = async (): Promise<Record<string, string>> => {
    const current = session ?? loadSession();
    if (!current) throw new Error('Session expired. Sign in again.');
    const live = await fresh(current);
    return { Authorization: `Bearer ${live.accessToken}` };
  };

  const formatMoney = (cents: number, currency: string = 'SAR') => {
    return (cents / 100).toLocaleString('en-SA', { style: 'currency', currency: currency });
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  };

  // --- Render Login ---
  if (!isAuthenticated) {
    return (
      <div className="login-screen">
        <div className="login-card glass-panel animate-fade-in">
          <div className="login-header">
            <div className="login-icon">
              <ShieldCheck size={28} />
            </div>
            <h1 className="login-title">Admin Dashboard</h1>
            <p className="login-subtitle">Law Agent Platform</p>
          </div>
          
          <form className="login-form" onSubmit={handleLogin}>
            <div className="input-group">
              <label className="input-label">Email</label>
              <input
                type="email"
                className="input-field"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@firm.example"
                autoComplete="username"
                required
              />
            </div>

            <div className="input-group">
              <label className="input-label">Password</label>
              <input
                type="password"
                className="input-field"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••••"
                autoComplete="current-password"
                required
              />
            </div>

            {authError && (
              <div className="error-message">
                <AlertCircle size={16} style={{display: 'inline', marginRight: 8}}/>
                {authError}
              </div>
            )}

            <button type="submit" className="submit-btn" disabled={isLoading || !email || !password}>
              {isLoading ? <Loader2 className="animate-spin" style={{margin: '0 auto'}}/> : 'Sign in'}
            </button>
          </form>
        </div>
      </div>
    );
  }

  // --- Render Dashboard ---
  return (
    <div className="app-container">
      <header className="header animate-fade-in">
        <div className="container header-content">
          <div className="brand">
            <ShieldCheck className="brand-icon" />
            Law Agent Admin
          </div>
          <nav className="top-nav">
            <button className={`nav-btn ${page === 'orders' ? 'is-active' : ''}`}
                    onClick={() => { setPage('orders'); setSelectedCase(null); }}>Orders</button>
            <button className={`nav-btn ${page === 'services' ? 'is-active' : ''}`}
                    onClick={() => { setPage('services'); setSelectedCase(null); }}>Services</button>
          </nav>
          <div className="user-controls">
            <button className="logout-btn" onClick={handleLogout}>
              <LogOut size={16} style={{marginRight: 6, display: 'inline', verticalAlign: 'text-bottom'}}/>
              Logout
            </button>
          </div>
        </div>
      </header>

      <main className="main-content">
        <div className="container">
          {page === 'services' ? (
            <Services backend={BACKEND} authHeaders={authHeaders} formatMoney={formatMoney} />
          ) : selectedCase ? (
            <CaseDetail
              consultationId={selectedCase}
              backend={BACKEND}
              authHeaders={authHeaders}
              onBack={() => setSelectedCase(null)}
              onChanged={() => { const s = session ?? loadSession(); if (s) loadData(s).catch(() => undefined); }}
              formatMoney={formatMoney}
              formatDate={formatDate}
            />
          ) : (
          <>
          <div className="page-header animate-fade-in delay-1">
            <h1 className="page-title">Overview</h1>
            <p className="page-subtitle">Orders and revenue, by service.</p>
          </div>

          {isLoading && !stats ? (
            <div className="loader-container">
              <Loader2 size={32} className="animate-spin" />
            </div>
          ) : stats ? (
            <div className="stats-grid animate-fade-in delay-2">
              <div className="stat-card glass-panel">
                <div className="stat-header">
                  <span>Total Revenue</span>
                  <CreditCard className="stat-icon" size={20} />
                </div>
                <div className="stat-value" style={{color: 'var(--success-text)'}}>
                  {formatMoney(stats.total_revenue_cents)}
                </div>
              </div>
              
              <div className="stat-card glass-panel">
                <div className="stat-header">
                  <span>Paid Orders</span>
                  <ShieldCheck className="stat-icon" size={20} />
                </div>
                <div className="stat-value">{stats.paid_consultations}</div>
              </div>

              <div className="stat-card glass-panel">
                <div className="stat-header">
                  <span>Pending Payment</span>
                  <Activity className="stat-icon" size={20} />
                </div>
                <div className="stat-value">{stats.pending_consultations}</div>
              </div>

              <div className="stat-card glass-panel">
                <div className="stat-header">
                  <span>All Orders</span>
                  <Users className="stat-icon" size={20} />
                </div>
                <div className="stat-value">{stats.total_consultations}</div>
              </div>
            </div>
          ) : null}

          {stats && stats.by_service.length > 0 && (
            <div className="service-strip animate-fade-in delay-2">
              {stats.by_service.map((b) => (
                <button
                  key={b.service_id}
                  className={`service-chip ${serviceFilter === b.service_id ? 'is-active' : ''}`}
                  onClick={() => setServiceFilter(serviceFilter === b.service_id ? '' : b.service_id)}
                  title="Filter the table by this service"
                >
                  <span className="service-chip-name" dir="rtl">{b.service_name}</span>
                  <span className="service-chip-meta">
                    {b.paid} paid · {formatMoney(b.revenue_cents)}
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="page-header animate-fade-in delay-3 orders-header" style={{marginTop: 48, marginBottom: 24}}>
            <h2 className="page-title" style={{fontSize: '1.5rem'}}>Recent Orders</h2>
            <div className="filters">
              <select className="input-field filter-select" value={statusFilter}
                      onChange={(e) => setStatusFilter(e.target.value)} aria-label="Status">
                <option value="">All statuses</option>
                <option value="pending">Pending</option>
                <option value="paid">Paid</option>
                <option value="in_progress">In progress</option>
                <option value="completed">Completed</option>
                <option value="cancelled">Cancelled</option>
                <option value="refunded">Refunded</option>
              </select>
              {serviceFilter && (
                <button className="open-btn" onClick={() => setServiceFilter('')}>Clear service filter</button>
              )}
            </div>
          </div>

          <div className="table-container animate-fade-in delay-3">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Client</th>
                  <th>Service</th>
                  <th>Amount</th>
                  <th>Status</th>
                  <th>Handover</th>
                  <th>Date</th>
                  <th><span className="sr-only">Open</span></th>
                </tr>
              </thead>
              <tbody>
                {consultations
                  .filter((c) => !serviceFilter || c.service_id === serviceFilter)
                  .filter((c) => !statusFilter || c.status === statusFilter)
                  .map(c => (
                  <tr
                    key={c.consultation_id}
                    className="row-clickable"
                    onClick={() => setSelectedCase(c.consultation_id)}
                  >
                    <td>
                      <div className="user-cell">
                        <span className="user-name">{c.user?.display_name || 'Anonymous User'}</span>
                        <span className="user-email">{c.user?.email || c.user?.phone || 'No contact'}</span>
                        {c.user?.wp_user_id != null && (
                          <span className="user-email" style={{ opacity: 0.75 }}>
                            WordPress #{c.user.wp_user_id}
                            {c.user.wp_role ? ` · ${c.user.wp_role}` : ''}
                          </span>
                        )}
                        {c.chat_language && (
                          <span className="badge badge-language">
                            Language: {c.chat_language}
                          </span>
                        )}
                        {c.escalation_summary && (
                          <div className="case-summary">
                            {c.escalation_summary}
                          </div>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className="service-name" dir="rtl">{c.service_name}</span>
                      {c.client_notes && (
                        <div className="case-summary" dir="rtl" title={c.client_notes}>{c.client_notes}</div>
                      )}
                    </td>
                    <td className="amount-cell">{formatMoney(c.amount_cents, c.currency)}</td>
                    <td>
                      <span className={`badge ${statusBadge(c.status)}`}>
                        {statusLabel(c.status)}
                      </span>
                    </td>
                    <td>
                      {c.status === 'pending' || c.status === 'cancelled' || c.status === 'refunded' ? (
                        <span className="badge badge-neutral">—</span>
                      ) : c.escalated ? (
                        <span className="badge badge-success">{c.needs_conversation ? 'Escalated' : 'Received'}</span>
                      ) : !c.ai_session_id || !c.needs_conversation ? (
                        // Bought without a conversation attached. There is
                        // nothing to hand over, so this is complete — not the
                        // "money taken, service not delivered" case the red
                        // badge is reserved for.
                        <span className="badge badge-neutral" title="Bought without a conversation">
                          No conversation
                        </span>
                      ) : (
                        <span className="badge badge-danger" title={c.escalation_error || 'Escalation has not succeeded yet'}>
                          Not handed over
                        </span>
                      )}
                    </td>
                    <td style={{color: 'var(--text-secondary)', fontSize: '0.875rem'}}>
                      {formatDate(c.created_at)}
                    </td>
                    <td>
                      {/* The row is clickable for a mouse; this is the same
                          action for a keyboard, which a <tr> cannot receive. */}
                      <button
                        className="open-btn"
                        onClick={(e) => { e.stopPropagation(); setSelectedCase(c.consultation_id); }}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
                
                {consultations.length === 0 && !isLoading && (
                  <tr>
                    <td colSpan={7} style={{textAlign: 'center', color: 'var(--text-muted)'}}>
                      No orders found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          </>
          )}
        </div>
      </main>
    </div>
  );
}

export default App;

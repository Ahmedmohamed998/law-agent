import { useState, useEffect, FormEvent } from 'react';
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

// --- Types ---
interface DashboardStats {
  total_consultations: number;
  paid_consultations: number;
  pending_consultations: number;
  failed_payments: number;
  total_revenue_cents: number;
}

interface User {
  id: string;
  email: string | null;
  display_name: string | null;
  phone: string | null;
  joined_at: string;
}

interface Payment {
  provider_txn_id: string;
  payment_status: string;
  payment_amount_cents: number;
}

interface Consultation {
  consultation_id: string;
  status: string;
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

const API_BASE = 'http://localhost:8001/admin/billing';

function App() {
  const [apiKey, setApiKey] = useState<string>(localStorage.getItem('adminApiKey') || '');
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [authError, setAuthError] = useState<string>('');
  
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [consultations, setConsultations] = useState<Consultation[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  // Auto-login if we have a key saved and it works
  useEffect(() => {
    if (apiKey) {
      checkAuthAndLoadData(apiKey);
    }
  }, []);

  const checkAuthAndLoadData = async (key: string) => {
    setIsLoading(true);
    setAuthError('');
    try {
      const headers = { 'X-Admin-Key': key };
      
      // Fetch stats
      const statsRes = await fetch(`${API_BASE}/stats`, { headers });
      if (!statsRes.ok) {
        if (statsRes.status === 403 || statsRes.status === 401) {
          throw new Error('Invalid Admin API Key');
        }
        throw new Error('Failed to load stats');
      }
      const statsData = await statsRes.json();
      
      // Fetch consultations
      const consultsRes = await fetch(`${API_BASE}/consultations`, { headers });
      if (!consultsRes.ok) throw new Error('Failed to load consultations');
      const consultsData = await consultsRes.json();
      
      setStats(statsData);
      setConsultations(consultsData);
      setIsAuthenticated(true);
      localStorage.setItem('adminApiKey', key);
    } catch (err: any) {
      setAuthError(err.message || 'Connection failed');
      setIsAuthenticated(false);
      localStorage.removeItem('adminApiKey');
    } finally {
      setIsLoading(false);
    }
  };

  const handleLogin = (e: FormEvent) => {
    e.preventDefault();
    checkAuthAndLoadData(apiKey);
  };

  const handleLogout = () => {
    setApiKey('');
    setIsAuthenticated(false);
    setStats(null);
    setConsultations([]);
    localStorage.removeItem('adminApiKey');
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
              <label className="input-label">Admin API Key</label>
              <input 
                type="password"
                className="input-field"
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
                placeholder="Enter secret key..."
                required
              />
            </div>
            
            {authError && (
              <div className="error-message">
                <AlertCircle size={16} style={{display: 'inline', marginRight: 8}}/>
                {authError}
              </div>
            )}
            
            <button type="submit" className="submit-btn" disabled={isLoading || !apiKey}>
              {isLoading ? <Loader2 className="animate-spin" style={{margin: '0 auto'}}/> : 'Authenticate'}
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
          <div className="page-header animate-fade-in delay-1">
            <h1 className="page-title">Overview</h1>
            <p className="page-subtitle">Real-time consultation and revenue metrics.</p>
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
                  <span>Paid Consultations</span>
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
                  <span>Total Users (Consults)</span>
                  <Users className="stat-icon" size={20} />
                </div>
                <div className="stat-value">{stats.total_consultations}</div>
              </div>
            </div>
          ) : null}

          <div className="page-header animate-fade-in delay-3" style={{marginTop: 48, marginBottom: 24}}>
            <h2 className="page-title" style={{fontSize: '1.5rem'}}>Recent Consultations</h2>
          </div>

          <div className="table-container animate-fade-in delay-3">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Client</th>
                  <th>Amount</th>
                  <th>Status</th>
                  <th>Escalation</th>
                  <th>Date</th>
                </tr>
              </thead>
              <tbody>
                {consultations.map(c => (
                  <tr key={c.consultation_id}>
                    <td>
                      <div className="user-cell">
                        <span className="user-name">{c.user?.display_name || 'Anonymous User'}</span>
                        <span className="user-email">{c.user?.email || c.user?.phone || 'No contact'}</span>
                        {c.chat_language && (
                          <span className="badge" style={{ marginTop: '4px', fontSize: '10px', backgroundColor: '#334155', color: '#cbd5e1' }}>
                            Language: {c.chat_language}
                          </span>
                        )}
                        {c.escalation_summary && (
                          <div style={{ marginTop: '8px', fontSize: '12px', color: '#94a3b8', maxWidth: '300px' }}>
                            {c.escalation_summary}
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="amount-cell">{formatMoney(c.amount_cents, c.currency)}</td>
                    <td>
                      <span className={`badge ${
                        c.status === 'paid' ? 'badge-success' : 
                        c.status === 'pending' ? 'badge-warning' : 'badge-danger'
                      }`}>
                        {c.status.toUpperCase()}
                      </span>
                    </td>
                    <td>
                      {c.status === 'paid' ? (
                        c.escalated ? (
                          <span className="badge badge-success">Escalated</span>
                        ) : (
                          <span className="badge badge-danger">Failed to Escalate</span>
                        )
                      ) : (
                        <span className="badge badge-neutral">—</span>
                      )}
                    </td>
                    <td style={{color: 'var(--text-secondary)', fontSize: '0.875rem'}}>
                      {formatDate(c.created_at)}
                    </td>
                  </tr>
                ))}
                
                {consultations.length === 0 && !isLoading && (
                  <tr>
                    <td colSpan={5} style={{textAlign: 'center', color: 'var(--text-muted)'}}>
                      No consultations found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Screen = 'loading' | 'login' | 'connected' | 'unavailable';

async function request(path: string, init?: RequestInit) {
  return fetch(path, { ...init, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(10_000) });
}

function App() {
  const [screen, setScreen] = useState<Screen>('loading');
  const [password, setPassword] = useState('');
  const [csrf, setCsrf] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const alert = useRef<HTMLParagraphElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (error) alert.current?.focus();
    else if (screen !== 'loading') heading.current?.focus();
  }, [error, screen]);

  async function showDemo(token: string) {
    const response = await request('/api/demo');
    if (response.status === 401) {
      setCsrf('');
      setScreen('login');
      setError('Your session ended. Sign in again.');
      return;
    }
    if (!response.ok) throw new Error('unavailable');
    const status = await response.json();
    setCsrf(token);
    setMessage(status.message);
    setScreen('connected');
  }

  async function restore() {
    setError('');
    setScreen('loading');
    try {
      const response = await request('/api/session');
      if (response.status === 401) { setScreen('login'); return; }
      if (!response.ok) throw new Error('unavailable');
      await showDemo((await response.json()).csrf);
    } catch {
      setScreen('unavailable');
      setError('Cannot reach the demo. Try again in a moment.');
    }
  }

  useEffect(() => { void restore(); }, []);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const response = await request('/api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
      });
      if (response.status === 401) { setError('That password was not accepted. Try again.'); return; }
      if (response.status === 429) { setError('Too many sign-in attempts. Wait one minute and try again.'); return; }
      if (response.status === 400) { setError('Use the presenter password configured for this deployment.'); return; }
      if (!response.ok) throw new Error('unavailable');
      setPassword('');
      await showDemo((await response.json()).csrf);
    } catch {
      setError('Cannot reach the demo. Try again in a moment.');
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      let response = await request('/api/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
      if (response.status === 403) {
        // Another tab may have replaced the shared cookie since this tab signed in.
        const current = await request('/api/session');
        if (current.ok) {
          const token = (await current.json()).csrf;
          setCsrf(token);
          response = await request('/api/logout', { method: 'POST', headers: { 'X-CSRF-Token': token } });
        } else {
          response = current;
        }
      }
      if (!response.ok && response.status !== 401) throw new Error('unavailable');
      setCsrf('');
      setScreen('login');
    } catch {
      setError('Sign-out did not complete. Try again in a moment.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <header>
        <p className="eyebrow">OCI application troubleshooter</p>
        <h1>Understand the incident.<br />See the recovery.</h1>
        <p className="intro">One place to investigate a sample application failure, approve a repair, and check the result.</p>
      </header>
      <section className="panel" aria-busy={busy || screen === 'loading'} aria-label="Demo access">
        {screen === 'loading' && <p role="status">Connecting to the demo…</p>}
        {screen === 'login' && <>
          <h2 ref={heading} tabIndex={-1}>Sign in to your demo</h2>
          <p>Use the presenter password set up for this deployment.</p>
          <form onSubmit={signIn}>
            <label htmlFor="password">Presenter password</label>
            <input id="password" name="password" type="password" autoComplete="current-password"
              required value={password} onChange={event => setPassword(event.target.value)} disabled={busy} />
            <button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
          </form>
        </>}
        {screen === 'connected' && <>
          <p className="badge">Signed in</p>
          <h2 ref={heading} tabIndex={-1}>Application connected</h2>
          <p>{message}</p>
          <button type="button" className="secondary" onClick={() => void signOut()} disabled={busy}>
            {busy ? 'Signing out…' : 'Sign out'}
          </button>
        </>}
        {screen === 'unavailable' && <>
          <h2 ref={heading} tabIndex={-1}>Connection unavailable</h2>
          <button type="button" onClick={() => void restore()}>Try again</button>
        </>}
        {error && <p className="error" role="alert" ref={alert} tabIndex={-1}>{error}</p>}
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);

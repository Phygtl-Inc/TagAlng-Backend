'use client';

import { getSupabase } from '@/lib/supabase';
import { useRouter } from 'next/navigation';
import { FormEvent, useState } from 'react';

const envMissing =
  !process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const supabase = getSupabase();
      const { error: signErr } = await supabase.auth.signInWithPassword({
        email,
        password,
      });
      if (signErr) {
        setError(signErr.message);
        return;
      }
      const { data: ok, error: adminErr } = await supabase.rpc('is_tagalng_admin');
      if (adminErr || !ok) {
        await supabase.auth.signOut();
        setError('Not on admin allowlist. Ask ops to add your user id.');
        return;
      }
      router.replace('/lana');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>Lana Admin</h1>
        <p>Lana inbox — email sign-in (ops allowlist)</p>
        {envMissing && (
          <p className="login-error">
            Create <code>apps/admin/.env.local</code> with NEXT_PUBLIC_SUPABASE_URL and
            NEXT_PUBLIC_SUPABASE_ANON_KEY (Supabase Dashboard → API → anon). Then restart{' '}
            <code>npm run dev</code>.
          </p>
        )}
        {error && <p className="login-error">{error}</p>}
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <button type="submit" disabled={loading}>
          {loading ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}

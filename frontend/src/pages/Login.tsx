import { useRef, useState, type FormEvent } from 'react'
import { ArrowRight, BrainCircuit, KeyRound, X } from 'lucide-react'

import { Button } from '../components/ui'
import { ThemeToggle } from '../components/ThemeToggle'

import { useAuth } from '../store/auth'
import { copy as t } from '../lib/copy'
import { useHealth } from '../hooks/queries'
import { LoginMemoryGraph } from './LoginMemoryGraph'
import { LoginParticles } from './LoginParticles'
import './Login.css'

export default function Login() {
  const login = useAuth((s) => s.login)
  const keyError = useAuth((s) => s.keyError)
  const health = useHealth()
  const keyInput = useRef<HTMLInputElement>(null)
  const [key, setKey] = useState('')

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (key.trim()) login(key)
  }

  const mode =
    health.isPending ? t.login.modeChecking
      : health.data?.ready ? t.login.modeApiKeys
        : t.login.modeUnavailable

  return (
    <div className="login-page relative grid min-h-screen place-items-center px-4">
      <LoginParticles />
      <ThemeToggle reveal className="absolute right-8 top-4 z-10" />

      <div className="login-shell w-full animate-fade-up">
        <section className="login-overview">
          <LoginMemoryGraph />
          <div className="login-brand">
            <span className="login-memory-icon" aria-hidden="true">
              <BrainCircuit />
            </span>
            <div>
              <div className="login-brand-name">{t.meta.brand}</div>
              <div className="login-brand-subtitle">{t.nav.consoleSub}</div>
            </div>
          </div>
          <h1 className="login-title">{t.login.title}</h1>
          <p className="login-intro text-ghost">{t.login.subtitle}</p>
        </section>

        <form onSubmit={submit} className="login-card" aria-label={t.login.title}>
          <p id="login-key-notice" className="login-notice text-ghost" aria-live="polite">
            {mode}
          </p>
          <div className="login-key-label">
            <label className="sr-only" htmlFor="login-api-key">{t.login.keyLabel}</label>
            <div className="login-key-field">
              <KeyRound className="h-4 w-4 text-ghost" />
              <input
                id="login-api-key"
                ref={keyInput}
                autoFocus
                aria-describedby={keyError ? 'login-key-notice login-key-error' : 'login-key-notice'}
                aria-invalid={!!keyError}
                className="w-full bg-transparent py-2.5 text-sm outline-none placeholder:text-ghost/70"
                placeholder={t.login.placeholder}
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              {key.length > 0 && (
                <button
                  type="button"
                  className="login-clear-key"
                  aria-label={t.login.clearKey}
                  title={t.login.clearKey}
                  onClick={() => {
                    setKey('')
                    keyInput.current?.focus()
                  }}
                >
                  <X aria-hidden="true" />
                </button>
              )}
            </div>
          </div>
          {keyError && (
            <p id="login-key-error" role="alert" className="mt-3 text-sm text-brand-rose">
              {keyError}
            </p>
          )}
          <Button type="submit" className="login-submit w-full" disabled={!key.trim()}>
            {t.login.submit} <ArrowRight className="h-4 w-4" />
          </Button>
        </form>
      </div>
    </div>
  )
}

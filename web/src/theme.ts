import { useEffect } from 'react'
import { useStore, Theme } from './store'

export type { Theme }

export function resolveDark(mode: Theme): boolean {
  if (mode === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  }
  return mode === 'dark'
}

// Hook: subscribe to store theme + system pref change, apply .dark class to <html>.
export function useThemeApplier() {
  const theme = useStore(s => s.theme)
  useEffect(() => {
    const apply = () => {
      document.documentElement.classList.toggle('dark', resolveDark(theme))
    }
    apply()
    if (theme === 'system') {
      const mq = window.matchMedia('(prefers-color-scheme: dark)')
      mq.addEventListener('change', apply)
      return () => mq.removeEventListener('change', apply)
    }
  }, [theme])
}

import { useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { X, Terminal as TermIcon } from 'lucide-react'
import { useStore } from '../store'
import { resolveDark } from '../theme'

const DARK_TERM_THEME = {
  background: '#0d1117',
  foreground: '#c9d1d9',
  cursor: '#58a6ff',
  selectionBackground: '#264f78',
  black: '#0d1117',
  red: '#ff7b72',
  green: '#3fb950',
  yellow: '#d29922',
  blue: '#58a6ff',
  magenta: '#bc8cff',
  cyan: '#39d353',
  white: '#c9d1d9',
}

const LIGHT_TERM_THEME = {
  background: '#ffffff',
  foreground: '#1f2328',
  cursor: '#0969da',
  selectionBackground: '#ddf4ff',
  black: '#1f2328',
  red: '#cf222e',
  green: '#1a7f37',
  yellow: '#9a6700',
  blue: '#0969da',
  magenta: '#8250df',
  cyan: '#1b7c83',
  white: '#57606a',
}

interface TerminalViewProps {
  terminalId: string
  provider?: string
  agentProfile?: string | null
  onClose: () => void
}

export function TerminalView({ terminalId, provider, agentProfile, onClose }: TerminalViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const theme = useStore(s => s.theme)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 12,
      fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, monospace',
      scrollback: 10000,
      theme: resolveDark(theme) ? DARK_TERM_THEME : LIGHT_TERM_THEME,
    })
    termRef.current = term

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(el)

    // Connect WebSocket
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${location.host}/terminals/${terminalId}/ws`)
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      // Fit once the connection is live so we send correct dimensions
      fitAddon.fit()
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
    }

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data))
      }
    }

    ws.onclose = () => {
      term.write('\r\n\x1b[33m[Connection closed]\x1b[0m\r\n')
    }

    // Copy selection to clipboard on mouse-up
    term.onSelectionChange(() => {
      const selection = term.getSelection()
      if (selection) {
        navigator.clipboard.writeText(selection).catch(() => {})
      }
    })

    // Ctrl+Shift+C to copy selection
    term.attachCustomKeyEventHandler((e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'C') {
        const selection = term.getSelection()
        if (selection) navigator.clipboard.writeText(selection).catch(() => {})
        return false
      }
      return true
    })

    // onData handles ALL input including paste — xterm.js
    // receives pasted text through the browser's input system
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }))
      }
    })

    // Handle resize — debounce to avoid flooding
    let resizeTimer: ReturnType<typeof setTimeout>
    const resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => {
        fitAddon.fit()
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
        }
      }, 50)
    })
    resizeObserver.observe(el)

    // Orientation change (mobile) — ResizeObserver can race with layout reflow
    let orientationTimer: ReturnType<typeof setTimeout>
    const handleOrientationChange = () => {
      clearTimeout(orientationTimer)
      orientationTimer = setTimeout(() => {
        fitAddon.fit()
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
        }
      }, 200)
    }
    window.addEventListener('orientationchange', handleOrientationChange)

    // Initial fit after layout settles
    const initialFit = requestAnimationFrame(() => {
      fitAddon.fit()
    })

    term.focus()

    return () => {
      cancelAnimationFrame(initialFit)
      clearTimeout(resizeTimer)
      clearTimeout(orientationTimer)
      resizeObserver.disconnect()
      window.removeEventListener('orientationchange', handleOrientationChange)
      ws.close()
      term.dispose()
      termRef.current = null
    }
  }, [terminalId])

  // Apply theme changes to the live terminal without recreating it
  useEffect(() => {
    if (termRef.current) {
      termRef.current.options.theme = resolveDark(theme) ? DARK_TERM_THEME : LIGHT_TERM_THEME
    }
  }, [theme])

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-gray-950">
      {/* Header */}
      <div className="flex items-center justify-between px-3 sm:px-4 py-2 bg-gray-900 border-b border-gray-700/50 shrink-0 gap-2">
        <div className="flex items-center gap-2 sm:gap-3 min-w-0">
          <TermIcon size={16} className="text-emerald-400 shrink-0" />
          <span className="text-xs sm:text-sm font-mono text-gray-300 truncate">{terminalId}</span>
          {provider && <span className="hidden sm:inline text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{provider}</span>}
          {agentProfile && <span className="hidden sm:inline text-xs text-emerald-400 bg-emerald-900/30 px-2 py-0.5 rounded">{agentProfile}</span>}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className="hidden sm:inline text-[10px] text-gray-600">Click X to close</span>
          <button
            onClick={onClose}
            className="tap text-gray-500 hover:text-gray-100 transition-colors rounded"
            title="Close terminal"
            aria-label="Close terminal"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      {/* Terminal — absolute positioning gives xterm.js real pixel dimensions to measure */}
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
      </div>
    </div>
  )
}

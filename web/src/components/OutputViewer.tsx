import { useState, useEffect, useRef } from 'react'
import { api } from '../api'
import { X, RefreshCw, Copy, Check, FileText, Loader2 } from 'lucide-react'

function stripAnsi(text: string): string {
  return text.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '')
}

interface OutputViewerProps {
  terminalId: string
  onClose: () => void
}

export function OutputViewer({ terminalId, onClose }: OutputViewerProps) {
  const [mode, setMode] = useState<'last' | 'full'>('last')
  const [output, setOutput] = useState('')
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)
  const outputRef = useRef<HTMLPreElement>(null)

  const fetchOutput = async (m: 'last' | 'full') => {
    setLoading(true)
    try {
      const data = await api.getTerminalOutput(terminalId, m)
      setOutput(data.output || '')
    } catch {
      setOutput('')
    }
    setLoading(false)
  }

  useEffect(() => {
    fetchOutput(mode)
  }, [mode, terminalId])

  // Auto-scroll to bottom on full output mode
  useEffect(() => {
    if (mode === 'full' && outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight
    }
  }, [output, mode])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const handleCopy = async () => {
    const clean = stripAnsi(output)
    try {
      await navigator.clipboard.writeText(clean)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // clipboard API may not be available
    }
  }

  const handleRefresh = () => {
    fetchOutput(mode)
  }

  const cleanOutput = stripAnsi(output)

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* Modal */}
      <div className="relative bg-gray-900 border border-gray-700/50 rounded-2xl shadow-2xl w-full max-w-[800px] mx-3 sm:mx-4 overflow-hidden animate-in fade-in zoom-in-95 flex flex-col" style={{ maxHeight: '90vh' }}>
        {/* Header */}
        <div className="flex items-center justify-between px-4 sm:px-6 py-4 border-b border-gray-700/30 shrink-0 gap-2">
          <div className="flex items-center gap-3 min-w-0">
            <FileText size={16} className="text-emerald-400 shrink-0" />
            <span className="text-sm font-semibold text-gray-100 shrink-0">Terminal Output</span>
            <span className="text-xs text-gray-500 font-mono bg-gray-800 px-2 py-0.5 rounded truncate">{terminalId}</span>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {/* Copy button */}
            <button
              onClick={handleCopy}
              disabled={!cleanOutput}
              className="tap text-gray-400 hover:text-gray-100 disabled:opacity-30 transition-colors rounded"
              title="Copy to clipboard"
            >
              {copied ? <Check size={16} className="text-emerald-400" /> : <Copy size={16} />}
            </button>
            {copied && <span className="hidden sm:inline text-xs text-emerald-400">Copied!</span>}
            {/* Refresh button */}
            <button
              onClick={handleRefresh}
              disabled={loading}
              className="tap text-gray-400 hover:text-gray-100 disabled:opacity-30 transition-colors rounded"
              title="Refresh output"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            {/* Close button */}
            <button
              onClick={onClose}
              className="tap text-gray-500 hover:text-gray-100 transition-colors rounded"
              title="Close"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Tab Toggle */}
        <div className="flex items-center gap-2 px-4 sm:px-6 py-3 border-b border-gray-700/30 shrink-0">
          <button
            onClick={() => setMode('last')}
            className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
              mode === 'last'
                ? 'bg-emerald-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700'
            }`}
          >
            Last Response
          </button>
          <button
            onClick={() => setMode('full')}
            className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
              mode === 'full'
                ? 'bg-emerald-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700'
            }`}
          >
            Full Output
          </button>
        </div>

        {/* Output Area */}
        <div className="flex-1 overflow-hidden px-4 py-3" style={{ minHeight: 0 }}>
          {loading ? (
            <div className="flex items-center justify-center h-full min-h-[200px]">
              <Loader2 size={24} className="animate-spin text-gray-500" />
            </div>
          ) : cleanOutput ? (
            <pre
              ref={outputRef}
              className="bg-gray-950 rounded-lg p-3 sm:p-4 font-mono text-xs sm:text-sm text-gray-300 overflow-y-auto whitespace-pre-wrap break-words"
              style={{ maxHeight: 'calc(90vh - 180px)' }}
            >
              {cleanOutput}
            </pre>
          ) : (
            <div className="flex items-center justify-center h-full min-h-[200px]">
              <p className="text-gray-500 text-sm">No output available</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

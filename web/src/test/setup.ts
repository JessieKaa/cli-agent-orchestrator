import '@testing-library/jest-dom'

// jsdom under vitest ships a broken localStorage (setItem is missing because
// --localstorage-file is passed without a valid path). Replace it with an
// in-memory polyfill so stores/middleware that touch localStorage work.
const memoryStore = new Map<string, string>()
const localStorageShim: Storage = {
  get length() { return memoryStore.size },
  clear: () => memoryStore.clear(),
  getItem: (k: string) => (memoryStore.has(k) ? memoryStore.get(k)! : null),
  key: (i: number) => Array.from(memoryStore.keys())[i] ?? null,
  removeItem: (k: string) => { memoryStore.delete(k) },
  setItem: (k: string, v: string) => { memoryStore.set(k, String(v)) },
}
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: localStorageShim,
})

// Data loading for pages that talk to the real CrisisGrid API (proxied under /cg).
import {useCallback, useEffect, useRef, useState} from 'react'
import {cg} from './auth'

/**
 * useCg(path, {every}) -> {data, err, loading, reload}
 *  - path null/undefined: nothing is fetched (e.g. no paper selected yet)
 *  - text: parse the response as text (for markdown reports) instead of JSON
 *  - every: poll interval in ms, or a function (data) => ms | 0 so a page can poll only while something is running
 *  - the previous data stays on screen while a reload is in flight (no flicker), and errors do not erase it
 */
export function useCg(path, {every = 0, text = false} = {}) {
  const [s, setS] = useState({data: null, err: null, loading: !!path})
  const [tick, setTick] = useState(0)
  const everyRef = useRef(every)
  everyRef.current = every

  useEffect(() => {
    if (!path) {
      setS({data: null, err: null, loading: false})
      return
    }
    let alive = true
    let timer
    setS(x => ({...x, loading: true, err: null}))
    cg(path, text ? {text: true} : {})
      .then(d => {
        if (!alive) return
        setS({data: d, err: null, loading: false})
        const ms = typeof everyRef.current === 'function' ? everyRef.current(d) : everyRef.current
        if (ms > 0) timer = setTimeout(() => setTick(t => t + 1), ms)
      })
      .catch(e => {
        if (!alive) return
        setS(x => ({...x, err: e, loading: false}))
        if (e.status == null || e.status >= 500) timer = setTimeout(() => setTick(t => t + 1), 3000) // backend restarting: retry
      })
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [path, tick])

  const reload = useCallback(() => setTick(t => t + 1), [])
  return {...s, reload}
}

export const errText = e => (e ? String(e.message || e) : '')

/** Download text as a file (used for the report). */
export function download(name, text, type = 'text/markdown') {
  const url = URL.createObjectURL(new Blob([text], {type}))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

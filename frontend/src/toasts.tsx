import {Check, X} from 'lucide-react';
import {createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode} from 'react';
import {Link} from 'react-router-dom';
import {t} from './locale';

type ToastAction = {to: string; label: string};
type Toast = {id: number; text: string; action?: ToastAction};
type Notify = (text: string, action?: ToastAction) => void;
const ToastContext = createContext<Notify | null>(null);
const visibleMs = 8000;

export function useToast() {
  const notify = useContext(ToastContext);
  if (!notify) throw new Error('ToastProvider is unavailable');
  return notify;
}

function ToastItem({toast, dismiss}: {toast: Toast; dismiss: (id: number) => void}) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (hovered || focused) return;
    const timer = window.setTimeout(() => dismiss(toast.id), visibleMs);
    return () => window.clearTimeout(timer);
  }, [toast.id, dismiss, hovered, focused]);
  return <li className="toast" onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onFocus={() => setFocused(true)} onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
    }}>
    <Check size={18} aria-hidden/>
    <div className="toast-body">
      <p>{t(toast.text)}</p>
      {toast.action && <Link to={toast.action.to} onClick={() => dismiss(toast.id)}>{t(toast.action.label)}</Link>}
    </div>
    <button className="toast-close" aria-label={t('Закрыть уведомление')} onClick={() => dismiss(toast.id)}>
      <X size={18} aria-hidden/>
    </button>
  </li>;
}

export function ToastProvider({children}: {children: ReactNode}) {
  const nextId = useRef(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [announcement, setAnnouncement] = useState<Toast>();
  const notify: Notify = useCallback((text, action) => {
    const item = {id: ++nextId.current, text, action};
    setToasts(current => [...current.slice(-2), item]);
    setAnnouncement(item);
  }, []);
  const dismiss = useCallback((id: number) => {
    setToasts(current => current.filter(item => item.id !== id));
    setAnnouncement(current => current?.id === id ? undefined : current);
  }, []);
  return <ToastContext.Provider value={notify}>
    {children}
    <div className="toast-announcement" role="status" aria-atomic="true">
      {announcement && <span key={announcement.id}>{t(announcement.text)}</span>}
    </div>
    {toasts.length > 0 && <aside className="toast-region" aria-label={t('Уведомления')}>
      <ul>{toasts.map(item => <ToastItem key={item.id} toast={item} dismiss={dismiss}/>)}</ul>
    </aside>}
  </ToastContext.Provider>;
}

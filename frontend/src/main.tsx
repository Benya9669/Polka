import {t, useLanguage} from './locale';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { BookCopy, BookOpen, BookOpenText, Download, FolderDown, FolderHeart, Globe2, House, LibraryBig, Search, Settings2, Tablet, UsersRound } from 'lucide-react';
import { lazy, Suspense, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes } from 'react-router-dom';
import { api, ApiError, json, type User } from './api';
import { AuthorPage, AuthorsPage } from './authors';
import { Catalog, WorkPage, Libraries, Operations, History, Settings } from './library';
import { Sources } from './sources';
import { ActivityPage, CollectionPage, Collections, Home, Shelf } from './personal';
import { LayoutPage, TrashPage } from './management';
import { SonyPage } from './sony';
import { SeriesDetail, SeriesPage } from './series';
import { OfflineShelf } from './offline-shelf';
import { clearCopies, identityKey, lastUser, offlineSupported, rememberUser } from './offline';
import { AppearanceSettings, LanguageSelect, useInterfaceTheme } from './appearance';
import { ToastProvider } from './toasts';
import './style.css';

const Reader = lazy(() => import('./reader'));
const query = new QueryClient({defaultOptions: {queries: {retry: false, staleTime: 1500}}});

export function Notice({error, children}: {error?: boolean; children: ReactNode}) {
  return <div className={error ? 'notice error' : 'notice'} role={error ? 'alert' : 'status'}>
    {typeof children === 'string' ? t(children) : children}
  </div>;
}

export function message(error: unknown) {
  return error instanceof Error ? t(error.message) : t("Не удалось выполнить операцию");
}

function Brand({to}: {to: string}) {
  return <Link className="brand" to={to}>
    <img src="/assets/polka-mark.svg" width="36" height="36" alt=""/>
    <span>{t("Полка")}</span>
  </Link>;
}

function SignIn({setup, requireKey}: {setup: boolean; requireKey: boolean}) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      const user = await api<User>(setup ? '/auth/setup' : '/auth/login', json('POST', data));
      rememberUser(user);
      query.setQueryData(['me'], user);
      await query.invalidateQueries({queryKey: ['setup']});
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return <main className="gate">
    <section className="gate-intro">
      <Brand to="/"/>
      <h1>{setup ? t("Ваша библиотека") : t("Снова за книгой")}</h1>
      <p>{setup ? t("Создайте учётную запись администратора, чтобы добавить первую книгу.") : t("Войдите в свою библиотеку, чтобы продолжить чтение.")}</p>
    </section>
    <form className="gate-form" onSubmit={submit}>
      <h2>{setup ? t("Первоначальная настройка") : t("Вход")}</h2>
      <label>{t("Имя пользователя")}<input name="username" required autoComplete="username" minLength={2} maxLength={64}/></label>
      <label>{t("Пароль")}<input name="password" type="password" required minLength={setup ? 10 : 1} autoComplete={setup ? 'new-password' : 'current-password'}/></label>
      {setup && requireKey && <label>{t("Ключ настройки")}<input name="setup_key" type="password" required autoComplete="off"/></label>}
      {error != null && <Notice error>{message(error)}</Notice>}
      <button className="primary" disabled={busy}>{busy ? t("Подождите…") : setup ? t("Создать аккаунт") : t("Войти")}</button>
      <LanguageSelect/>
    </form>
  </main>;
}

function App() {
  const language = useLanguage();
  const appearance = useInterfaceTheme();
  useEffect(() => {
    document.documentElement.lang = language;
    document.title = t("Полка — электронная библиотека");
  }, [language]);
  const me = useQuery({queryKey:['me'], networkMode:'always', refetchOnReconnect:'always', queryFn:async () => {
    try {
      const user = await api<User>('/auth/me');
      rememberUser(user);
      return user;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) rememberUser();
      const saved = error instanceof TypeError ? lastUser() : undefined;
      if (saved && offlineSupported()) return {...saved, offline:true};
      throw error;
    }
  }});
  const status = useQuery({queryKey: ['setup'], queryFn: () => api<{setup: boolean; setup_key_required: boolean}>('/auth/status'),
    enabled: me.error instanceof ApiError && me.error.status === 401});
  useEffect(() => {
    const switchAccount = (event: StorageEvent) => {
      if (event.key === identityKey && me.data && lastUser()?.id !== me.data.id) location.reload();
    };
    window.addEventListener('storage',switchAccount);
    return () => window.removeEventListener('storage',switchAccount);
  }, [me.data]);
  if (!me.data || me.error instanceof ApiError && me.error.status === 401) {
    if (status.data) return <SignIn setup={status.data.setup} requireKey={status.data.setup_key_required}/>;
    if (status.error || (me.error && !(me.error instanceof ApiError && me.error.status === 401))) {
      return <main className="loading"><Notice error>{message(status.error || me.error)}</Notice><button onClick={() => location.reload()}>{t("Повторить")}</button></main>;
    }
    return <main className="loading" aria-busy="true">{t("Открываем библиотеку…")}</main>;
  }
  const user = me.data;
  const offline = 'offline' in user && user.offline === true;
  const offlineNav = ['/offline', t("Книги без сети"), FolderDown] as const;
  const nav = offline ? [offlineNav] : [
    ['/home', t("Главная"), House], ['/', t("Все книги"), BookOpen], ['/authors', t("Авторы"), UsersRound], ['/series', t("Серии"), BookCopy],
    ['/shelf', t("Мои книги"), BookOpenText], ['/collections', t("Коллекции"), FolderHeart], ['/libraries', t("Библиотеки"), LibraryBig],
    ['/sources', t("OPDS-каталоги"), Globe2], ['/sources/search', t("Поиск по источникам"), Search], ['/downloads', t("Загрузки и импорт"), Download],
    ['/devices/sony', 'Sony PRS-600', Tablet], offlineNav, ['/settings', t("Настройки"), Settings2],
  ] as const;
  if (offline && !/^\/(?:offline\/?|read\/\d+\/?)$/.test(location.pathname)) return <Navigate to="/offline" replace/>;
  return <ToastProvider key={user.id}><div className="shell">
    <aside className="sidebar">
      <Brand to={offline ? '/offline' : '/home'}/>
      <nav aria-label={t("Разделы")}>{nav.map(([to, label, Icon]) => <NavLink key={to} to={to} end={to === '/' || to === '/sources'}>
        <Icon aria-hidden size={18}/><span>{label}</span></NavLink>)}</nav>
      <div className="account">{user.username}</div>
    </aside>
    <main className="main">
      <Routes>
        <Route path="/" element={<Catalog user={user}/>}/>
        <Route path="/home" element={<Home/>}/>
        <Route path="/activity" element={<ActivityPage/>}/>
        <Route path="/shelf" element={<Shelf/>}/>
        <Route path="/collections" element={<Collections/>}/>
        <Route path="/collections/:id" element={<CollectionPage key={location.pathname}/>}/>
        <Route path="/books/:id" element={<WorkPage user={user}/>}/>
        <Route path="/authors" element={<AuthorsPage/>}/>
        <Route path="/authors/:id" element={<AuthorPage key={location.pathname} user={user}/>}/>
        <Route path="/series" element={<SeriesPage/>}/>
        <Route path="/series/:id" element={<SeriesDetail/>}/>
        <Route path="/read/:id" element={<Suspense fallback={<p>{t("Открываем книгу…")}</p>}><Reader user={user}/></Suspense>}/>
        <Route path="/offline" element={<OfflineShelf user={user}/>}/>
        <Route path="/continue" element={<History/>}/>
        <Route path="/libraries" element={<Libraries user={user}/>}/>
        <Route path="/libraries/:id/layout" element={<LayoutPage key={location.pathname}/>}/>
        <Route path="/trash" element={<TrashPage/>}/>
        <Route path="/sources" element={<Sources key="catalogues" user={user}/>}/>
        <Route path="/sources/search" element={<Sources key="search" user={user} searchPage/>}/>
        <Route path="/downloads" element={<Operations/>}/>
        <Route path="/devices/sony" element={<SonyPage key={location.search}/>}/>
        <Route path="/settings" element={<Settings user={user} appearance={<AppearanceSettings {...appearance}/>} logout={async () => {
          await api('/auth/logout', json('POST'));
          rememberUser();
          if (offlineSupported()) await clearCopies(user.id).catch(() => {});
          query.clear();
          location.assign('/');
        }}/>}/>
        <Route path="*" element={<><h1>{t("Страница не найдена")}</h1><Link to="/">{t("Вернуться в каталог")}</Link></>}/>
      </Routes>
    </main>
  </div></ToastProvider>;
}

createRoot(document.getElementById('root')!).render(<QueryClientProvider client={query}><BrowserRouter><App/></BrowserRouter></QueryClientProvider>);


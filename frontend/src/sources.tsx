import {t} from './locale';
import {useQuery} from '@tanstack/react-query';
import {ChevronLeft, ChevronRight, Globe2, Plus, Search} from 'lucide-react';
import {useState, type FormEvent, type MouseEvent} from 'react';
import {Link, useNavigate} from 'react-router-dom';
import {api, json, type User, type Work} from './api';
import {BookGrid, formatBytes, useLibraries} from './library';
import {message, Notice} from './main';
import {useToast} from './toasts';

type Source = {id: number; name: string; url: string; description: string; enabled: boolean; authenticated: boolean};
type Publication = {title: string; authors: string[]; summary: string; language: string; cover: string | null; purchase: boolean;
  series_memberships: {path: string[]; position: string}[]; downloads: {type: string; link: string; size: number | null}[];
  matches?: {local: {id: number; title: string; library: string}[]; local_total: number;
    sources: {id: number; name: string}[]}};
type DownloadReview = {title: string; source: string; format: string; size: number | null;
  matches: {work_id: number; title: string; confidence: number; reason: string}[]};
type Feed = {title: string; version: string; search: boolean; next: string | null; previous: string | null;
  facets: {title: string; sorting: boolean; options: {title: string; active: boolean; count: number | null;
    sort: 'new' | 'popular' | null; link: string}[]}[];
  navigation: {title: string; link: string}[]; publications: Publication[]};
type GlobalResult = {local: {items: Work[]}; remote: {source: Source; result?: Feed; error?: string; unsupported?: boolean}[]};
type Authentication = {secure: boolean; same_origin: boolean; protected: boolean; document: {
  title: string; description: string;
  methods: {type: string; kind: 'basic' | 'bearer' | null; login: string; password: string}[];
  links: {help?: string; register?: string; logo?: string};
} | null};
const endpoint = '/opds/sources';
const labels: Record<string, string> = {'application/epub+zip': 'EPUB', 'application/x-fictionbook+xml':'FB2',
  'application/pdf':'PDF', 'application/zip':'ZIP', 'text/plain':'TXT', 'application/x-mobipocket-ebook':'MOBI'};

export function Sources({user, searchPage = false}: {user: User; searchPage?: boolean}) {
  const notify = useToast();
  const navigate = useNavigate();
  const sources = useQuery({queryKey: ['sources'], queryFn: () => api<Source[]>(endpoint), enabled: !searchPage});
  const libraries = useLibraries();
  const destinations = libraries.data?.filter(library => !user.read_only && library.mode === 'managed'
    && (user.admin || library.owner_id === user.id)) || [];
  const [destination, setDestination] = useState('');
  const [editing, setEditing] = useState<Source | 'new'>();
  const [auth, setAuth] = useState('');
  const [authentication, setAuthentication] = useState<Authentication>();
  const [selected, setSelected] = useState<Source>();
  const [feed, setFeed] = useState<Feed>();
  const [history, setHistory] = useState<Feed[]>([]);
  const [global, setGlobal] = useState<GlobalResult>();
  const [globalLoading, setGlobalLoading] = useState(false);
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [busy, setBusy] = useState(false);
  const [review,setReview] = useState<{params: {source_id: number; library_id: number; link: string}; data: DownloadReview}>();
  const [target,setTarget] = useState('');
  const basic = authentication?.document?.methods.find(method => method.kind === 'basic');

  async function run(action: () => Promise<void>) {
    setNotice(undefined);
    setBusy(true);
    try { await action(); } catch (e) {setNotice({text: message(e), error: true});} finally {setBusy(false);}
  }

  function open(source: Source, link?: string, term?: string) {
    setReview(undefined);
    void run(async () => {
      const data = term
        ? await api<Feed>(endpoint + '/' + source.id + '/search?q=' + encodeURIComponent(term))
        : await api<Feed>(endpoint + '/' + source.id + '/browse', json('POST', {link}));
      setHistory(selected?.id === source.id && feed && (link || term) ? [...history, feed] : []);
      setSelected(source);
      setFeed(data);
      setGlobal(undefined);
      setEditing(undefined);
    });
  }

  function edit(source: Source | 'new') {
    setReview(undefined);
    setSelected(undefined);
    setFeed(undefined);
    setGlobal(undefined);
    setEditing(source);
    setAuth('');
    setAuthentication(undefined);
  }

  function discoverAuth(event: MouseEvent<HTMLButtonElement>) {
    const form = event.currentTarget.form;
    const input = form?.elements.namedItem('url');
    if (!(input instanceof HTMLInputElement) || !input.reportValidity()) return;
    const url = input.value;
    setAuthentication(undefined);
    void run(async () => {
      const result = await api<Authentication>(endpoint + '/authentication', json('POST', {url}));
      if (!input.isConnected || input.value !== url) return;
      setAuthentication(result);
      if (result.secure && result.same_origin && (editing === 'new' || !editing?.authenticated)) {
        const preferred = result.document?.methods.find(method => method.kind === 'basic')
          || result.document?.methods.find(method => method.kind === 'bearer');
        if (preferred?.kind) setAuth(current => current || preferred.kind || '');
      }
    });
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const payload = {...Object.fromEntries(values), enabled: values.has('enabled'), auth: auth || null};
    void run(async () => {
      const id = editing && editing !== 'new' ? editing.id : undefined;
      await api(id ? endpoint + '/' + id : endpoint, json(id ? 'PUT' : 'POST', payload));
      await sources.refetch();
      setEditing(undefined);
      notify("Источник сохранён");
    });
  }

  function publications(books: Publication[], source: Source) {
    return <ul className="rows">{books.map((book, index) => <li key={index}>
      <div className="remote-book">
        {book.cover && <img src={book.cover} alt="" loading="lazy" onError={event => {event.currentTarget.hidden = true;}}/>}
        <div><strong>{book.title || t("Без названия")}</strong><small>{[book.authors.filter(Boolean).join(', '), book.language].filter(Boolean).join(' · ')}</small>
        {book.series_memberships?.map(series => <small key={series.path.join('/')}>
          {series.path.join(' / ')}{series.position && t(" · Том ") + series.position}</small>)}
        {book.summary && <details><summary>{t("Описание")}</summary><p>{book.summary}</p></details>}
        {book.matches && book.matches.local.length > 0 && <div className="opds-matches">
          <small>{t("Возможные совпадения в библиотеке")}</small>
          <ul>{book.matches.local.map(work => <li key={work.id}>
            <Link to={'/books/' + work.id}>{work.title}</Link><small>{work.library}</small>
          </li>)}</ul>
          {book.matches.local_total > book.matches.local.length &&
            <small>{t("Показано ")}{book.matches.local.length}{t(" из ")}{book.matches.local_total}</small>}
        </div>}
        {book.matches && book.matches.sources.length > 0 &&
          <small>{t("Похожая запись есть в каталогах ")}{book.matches.sources.map(entry => entry.name).join(', ')}</small>}
        {!book.downloads.length && <small>{book.purchase ? t("Доступно у источника для покупки") : t("Доступно для просмотра")}</small>}
        </div>
      </div>
      <div className="actions">{book.downloads.map(item => {
        const format = labels[item.type.split(';')[0]] || t("файл");
        return <div className="actions" key={item.link}>
          <button disabled={busy || user.read_only} aria-label={t("Скачать {format} без импорта", {format})} onClick={() => run(async () => {
            const job = await api<{id:number}>('/opds/downloads', json('POST', {source_id:source.id,link:item.link,delivery:'file'}));
            notify("Файл добавлен в очередь");
            navigate('/downloads?file=' + job.id);
          })}>{t("Скачать ")}{format}</button>
          <button className="primary" disabled={busy || !destinations.length} onClick={() => run(async () => {
            const params = {source_id:source.id, library_id:Number(destination || destinations[0].id), link:item.link};
            const data = await api<DownloadReview>('/opds/downloads/preview',json('POST',params));
            setTarget('');
            setReview({params,data});
            requestAnimationFrame(() => document.getElementById('download-review')?.focus());
          })}>{format === t("файл") ? t("Импорт файла") : t("Импорт {format}", {format})}
            {item.size ? ' · ' + formatBytes(item.size) : ''}</button>
        </div>;
      })}</div>
    </li>)}</ul>;
  }

  const availability = [
    {heading: t("Можно скачать"), downloadable: true},
    {heading: t("Просмотр или покупка"), downloadable: false},
  ];
  const unavailable = global?.remote.filter(entry => entry.error) || [];
  const unsupported = global?.remote.filter(entry => entry.unsupported) || [];

  return <>
    <header className="heading"><div><h1>{searchPage ? t("Поиск по источникам") : t("OPDS-каталоги")}</h1>
      {searchPage && <p>{t("Поиск в библиотеке и подключённых каталогах")}</p>}</div>
      {user.admin && !searchPage && <button className="primary" onClick={() => edit('new')} disabled={busy}><Plus size={18}/>{t(" Добавить источник")}</button>}</header>
    <form className="filters" onSubmit={event => {
      event.preventDefault();
      const q = String(new FormData(event.currentTarget).get('q')).trim();
      if (!q || busy) return;
      setGlobalLoading(true);
      void run(async () => {
        try {
          const data = await api<GlobalResult>('/opds/search?q=' + encodeURIComponent(q));
          setGlobal(data);
          setEditing(undefined);
          setSelected(undefined);
          setFeed(undefined);
          setReview(undefined);
        } finally {
          setGlobalLoading(false);
        }
      });
    }}>
      <label className="search"><Search size={18} aria-hidden/><input aria-label={t("Поиск во всех каталогах")} name="q" type="search" required
        maxLength={300} placeholder={t("Поиск во всех каталогах")}/></label>
      <button disabled={busy}>{globalLoading ? t("Ищем…") : t("Искать везде")}</button>
    </form>
    {globalLoading && <p role="status">{t("Ищем по библиотеке и внешним каталогам…")}</p>}
    {(sources.error || notice) && <Notice error={Boolean(sources.error || notice?.error)}>{notice?.text || message(sources.error)}</Notice>}
    <div className={searchPage ? undefined : 'split sources'}>
      {!searchPage && <div>
        {sources.data?.length === 0 && <section className="panel"><Globe2 size={26}/><h2>{t("Источников пока нет")}</h2>
          <p>{user.admin ? t("Добавьте URL каталога OPDS 1.x или 2.0.") : t("Попросите администратора добавить источник.")}</p></section>}
        <ul className="rows source-list">{sources.data?.map(source => <li key={source.id}>
          <div><strong>{source.name}</strong><small>{source.description || source.url}</small>
            {!source.enabled && <small>{t("Отключён")}</small>}</div>
          <div className="actions">
            <button disabled={!source.enabled || busy} onClick={() => open(source)}>{t("Открыть")}</button>
            {user.admin && <button disabled={busy} onClick={() => edit(source)}>{t("Изменить")}</button>}
          </div>
        </li>)}</ul>
      </div>}
      <div>
        {editing && <form className="panel fields" key={editing === 'new' ? 'new' : editing.id} onSubmit={save}>
          <h2>{editing === 'new' ? t("Новый источник") : editing.name}</h2>
          <label>{t("Название")}<input name="name" required maxLength={120} defaultValue={editing !== 'new' ? editing.name : ''}/></label>
          <label>{t("URL каталога")}<input name="url" type="url" required maxLength={5000} defaultValue={editing !== 'new' ? editing.url : ''}
            onChange={() => setAuthentication(undefined)}/></label>
          <div className="actions"><button type="button" disabled={busy} onClick={discoverAuth}>{t("Определить способ входа")}</button></div>
          {authentication && <div className="source-auth">
            {authentication.document && <div className="source-auth-profile">
              {authentication.document.links.logo && <img src={authentication.document.links.logo} alt=""
                onError={event => {event.currentTarget.hidden = true;}}/>}
              <div>
                {authentication.document.title && <strong>{authentication.document.title}</strong>}
                {authentication.document.description && <p>{authentication.document.description}</p>}
                <div className="actions">
                  {authentication.document.links.help && <a href={authentication.document.links.help} target="_blank" rel="noopener noreferrer">{t("Помощь со входом")}</a>}
                  {authentication.document.links.register && <a href={authentication.document.links.register} target="_blank" rel="noopener noreferrer">{t("Регистрация")}</a>}
                </div>
              </div>
            </div>}
            <div role="status">
              {!authentication.document && (authentication.protected ? t("Источник требует входа. Способ входа не указан.") : t("Каталог доступен без входа."))}
              {authentication.document?.methods.map((method, index) => <div key={index}>
                {method.kind === 'basic' ? 'HTTP Basic' : method.kind === 'bearer' ? t("Bearer, готовый токен") : t("Способ входа не поддерживается: ") + method.type}
              </div>)}
              {authentication.document?.methods.length === 0 && t("Источник не объявил способы входа.")}
              {authentication.document && !authentication.secure && <div>{t("Для передачи учётных данных укажите HTTPS-адрес каталога.")}</div>}
              {authentication.document && !authentication.same_origin && <div>{t("Каталог перенаправляет на другой сервер. Укажите его адрес для входа.")}</div>}
            </div>
          </div>}
          <label>{t("Описание")}<textarea name="description" maxLength={2000} defaultValue={editing !== 'new' ? editing.description : ''}/></label>
          <label>{t("Авторизация")}<select value={auth} onChange={event => setAuth(event.target.value)}>
            <option value="">{editing !== 'new' && editing.authenticated ? t("Сохранить текущую") : t("Без авторизации")}</option>
            {editing !== 'new' && editing.authenticated && <option value="none">{t("Удалить учётные данные")}</option>}
            <option value="basic">{t("HTTP Basic через HTTPS")}</option><option value="bearer">{t("Bearer через HTTPS")}</option>
          </select></label>
          {auth === 'basic' && <label>{basic?.login || t("Имя пользователя")}<input name="username" required autoComplete="off"/></label>}
          {['basic','bearer'].includes(auth) && <label>{auth === 'basic' ? basic?.password || t("Пароль") : t("Токен")}<input required name="password" type="password" autoComplete="new-password"/></label>}
          <label className="check"><input type="checkbox" name="enabled" defaultChecked={editing === 'new' || editing.enabled}/>{t(" Источник включён")}</label>
          <div className="actions">
            <button className="primary" disabled={busy}>{t("Сохранить")}</button>
            {editing !== 'new' && <>
              <button type="button" disabled={busy} onClick={() => run(async () => {
                const result = await api<{title: string; version: string}>(endpoint + '/' + editing.id + '/test', json('POST'));
                setNotice({text: result.title + ' · OPDS ' + result.version});
              })}>{t("Проверить")}</button>
              <button type="button" disabled={busy} onClick={() => {
                if (confirm(t("Удалить источник «{name}»?", {name: editing.name}))) void run(async () => {
                  await api(endpoint + '/' + editing.id, json('DELETE'));
                  await sources.refetch();
                  setEditing(undefined);
                });
              }}>{t("Удалить")}</button>
            </>}
          </div>
        </form>}
        {(feed || global) && <div className="destination">
          {destinations.length ? <label>{t("Добавлять в")}<select value={destination || String(destinations[0].id)} onChange={event => {
            setDestination(event.target.value); setReview(undefined);
          }}>
            {destinations.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}</select></label>
            : user.read_only ? <Notice>{t("Эта учётная запись может просматривать каталоги.")}</Notice>
            : <Notice>{t("Для импорта ")}<Link to="/libraries">{t("создайте управляемую библиотеку")}</Link>.</Notice>}
        </div>}
        {review && <section className="panel fields" id="download-review" tabIndex={-1} aria-labelledby="download-review-title">
          <h2 id="download-review-title">{review.data.title || t("Импорт книги")}</h2>
          <p>{[review.data.source, review.data.format.toUpperCase(), review.data.size ? formatBytes(review.data.size) : '',
            destinations.find(lib => lib.id === review.params.library_id)?.name].filter(Boolean).join(' · ')}</p>
          {!!review.data.matches.length && <>
            <label>{t("Карточка для импорта")}<select aria-label={t("Карточка для импорта")} value={target} onChange={event => setTarget(event.target.value)}>
              <option value="">{t("Создать отдельную карточку")}</option>
              {review.data.matches.map(match => <option key={match.work_id} value={match.work_id}>{match.title} · {match.confidence}/100</option>)}
            </select></label>
            {target && <small>{t(review.data.matches.find(match => String(match.work_id) === target)?.reason || '')}
              {t(". Файл будет добавлен отдельным изданием.")}</small>}
          </>}
          <div className="actions">
            <button disabled={busy} onClick={() => setReview(undefined)}>{t("Назад")}</button>
            {[false,true].map(read => <button key={String(read)} className={read ? undefined : 'primary'} disabled={busy} onClick={() => run(async () => {
              const job = await api<{id: number}>('/opds/downloads',json('POST',{
                ...review.params,work_id:target ? Number(target) : null,read_after:read,
              }));
              setReview(undefined);
              if (read) {
                notify("Книга добавлена в очередь");
                navigate('/downloads?read=' + job.id);
              } else {
                notify("Книга добавлена в очередь", {to:'/downloads', label:"Открыть загрузки"});
              }
            })}>{read ? t("Импортировать и читать") : t("Добавить в очередь")}</button>)}
          </div>
        </section>}
        {selected && feed && <section className="panel">
          <div className="heading compact"><div><h2>{feed.title || selected.name}</h2><p>OPDS {feed.version}</p></div>
            {history.length > 0 && <button disabled={busy} onClick={() => {
              setFeed(history[history.length-1]); setHistory(history.slice(0, -1));
            }}><ChevronLeft size={16}/>{t(" Назад")}</button>}</div>
          {feed.search && <form className="filters" onSubmit={event => {
            event.preventDefault(); const q = String(new FormData(event.currentTarget).get('q')).trim();
            if (q) open(selected, undefined, q);
          }}>
            <input name="q" aria-label={t("Поиск в каталоге")} type="search" maxLength={300} required placeholder={t("Поиск в каталоге")}/>
            <button disabled={busy}>{t("Найти")}</button>
          </form>}
          {feed.facets?.length > 0 && <div className="actions opds-facets">
            {feed.facets.map((group, index) => <label key={index}>
              {group.sorting ? t("Сортировка") : group.title || t("Параметры каталога")}
              <select disabled={busy} value={group.options.find(option => option.active)?.link || ''} onChange={event => {
                if (event.target.value) open(selected, event.target.value);
              }}>
                <option value="" disabled>{t("Выберите вариант")}</option>
                {group.options.map(option => <option value={option.link} key={option.link}>
                  {option.title || (option.sort === 'new' ? t("Новые поступления")
                    : option.sort === 'popular' ? t("Популярные") : t("Вариант"))}
                  {option.count !== null ? ' · ' + option.count : ''}
                </option>)}
              </select>
            </label>)}
          </div>}
          {feed.navigation.length > 0 && <ul className="rows">{feed.navigation.map(item => <li key={item.link}>
            <button className="text-button" disabled={busy} onClick={() => open(selected, item.link)}>{item.title || t("Раздел")} <ChevronRight size={16}/></button>
          </li>)}</ul>}
          {publications(feed.publications, selected)}
          {!feed.publications.length && !feed.navigation.length && <p>{t("Книги и разделы не найдены.")}</p>}
          {(feed.previous || feed.next) && <div className="actions">
            {feed.previous && <button disabled={busy} onClick={() => open(selected, feed.previous || undefined)}>
              <ChevronLeft size={16}/>{t("Предыдущая страница")}
            </button>}
            {feed.next && <button disabled={busy} onClick={() => open(selected, feed.next || undefined)}>
              {t("Следующая страница")}<ChevronRight size={16}/>
            </button>}
          </div>}
        </section>}
        {global && <section>
          <h2>{t("Уже есть в библиотеке")}</h2>
          {global.local.items.length ? <BookGrid books={global.local.items}/> : <p>{t("Совпадений нет.")}</p>}
          {availability.map(({heading, downloadable}) => {
            const groups = global.remote.flatMap(entry => {
              const books = entry.result?.publications.filter(book => (book.downloads.length > 0) === downloadable) || [];
              return books.length ? [{...entry, books}] : [];
            });
            return <section className="opds-results" key={heading}><h2>{heading}</h2>
              {groups.length ? groups.map(entry => <section className="panel" key={entry.source.id}>
                <h3>{entry.source.name}</h3>
                {publications(entry.books, entry.source)}
                {entry.result?.next && <button disabled={busy} onClick={() => open(entry.source, entry.result?.next || undefined)}>{t("Следующая страница в каталоге ")}<ChevronRight size={16}/>
                </button>}
              </section>) : <p>{t("Совпадений нет.")}</p>}
            </section>;
          })}
          {unavailable.length > 0 && <section className="opds-results"><h2>{t("Источники недоступны")}</h2>
            <ul className="rows">{unavailable.map(entry => <li key={entry.source.id}>
              <div><strong>{entry.source.name}</strong><small>{t(entry.error || '')}</small></div>
            </li>)}</ul>
          </section>}
          {unsupported.length > 0 && <section className="opds-results"><h2>{t("Каталоги без поиска")}</h2>
            <ul className="rows">{unsupported.map(entry => <li key={entry.source.id}><strong>{entry.source.name}</strong>
              <button disabled={busy} onClick={() => open(entry.source)}>{t("Открыть каталог")}</button>
            </li>)}</ul>
          </section>}
        </section>}
      </div>
    </div>
  </>;
}


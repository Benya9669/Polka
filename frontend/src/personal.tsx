import {currentLanguage, t} from './locale';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {BookCheck, Heart} from 'lucide-react';
import {useState} from 'react';
import {Link, useParams, useSearchParams} from 'react-router-dom';
import {api, json, type Work} from './api';
import {BookGrid, Cover} from './library';
import {message, Notice} from './main';
import {useToast} from './toasts';

const statuses = {
  unread: 'Не начато', planned: 'Хочу прочитать', reading: 'Читаю', paused: 'Приостановлено',
  finished: 'Прочитано', abandoned: 'Брошено', rereading: 'Перечитываю',
} as const;

type ReadingState = {
  status: keyof typeof statuses; favorite: boolean; started: string | null; finished: string | null;
  last_read: string | null; last_file_id: number | null; percentage: number | null; device: string | null; chapter: string | null;
};
type Collection = {id: number; name: string; contains: boolean};
type ShelfItem = {book: Work; state: ReadingState};
type Overview = {reading: ShelfItem[]; recent: Work[]; statistics: {
  finished: number; reading: number; finished_this_month: number; seconds: number;
}};
type Activity = {file_id: number; work_id: number; title: string; opened: string; seconds: number};
const views = {reading: 'Читаю', planned: 'Хочу прочитать', finished: 'Прочитано', favorites: 'Избранное'} as const;

function dateTime(stamp: string) {
  return /(Z|[+-]\d{2}:\d{2})$/.test(stamp) ? stamp : stamp + 'Z';
}

function date(stamp: string) {
  return new Date(dateTime(stamp)).toLocaleDateString(currentLanguage());
}

function useCollections(workId?: number) {
  return useQuery({queryKey: ['collections', workId], queryFn: () =>
    api<Collection[]>('/collections' + (workId ? '?work_id=' + workId : ''))});
}

export function ReadingControls({work}: {work: Work}) {
  const client = useQueryClient();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const query = useQuery({queryKey: ['reading-state', work.id], queryFn: () => api<ReadingState>('/reading/states/' + work.id)});
  const collections = useCollections(work.id);
  const state = query.data;
  async function change(path: string, verb: string, body?: unknown) {
    setError('');
    setBusy(true);
    try {
      await api(path, json(verb, body));
      await Promise.all([query.refetch(), client.invalidateQueries({queryKey: ['reading-home']}),
        client.invalidateQueries({queryKey: ['reading-shelf']}), client.invalidateQueries({queryKey: ['collections']}),
        client.invalidateQueries({queryKey: ['collection']})]);
    } catch (e) {setError(message(e));} finally {setBusy(false);}
  }
  const path = '/reading/states/' + work.id;
  return <section className="personal-controls">
    {state && <div className="controls">
      <label>{t("Статус чтения ")}<select disabled={busy} value={state.status} onChange={e => change(path, 'PATCH', {status: e.target.value})}>
        {Object.entries(statuses).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}
      </select></label>
      <button type="button" disabled={busy} aria-pressed={state.favorite} onClick={() => change(path, 'PATCH', {favorite: !state.favorite})}>
        <Heart size={16} fill={state.favorite ? 'currentColor' : 'none'}/>{t(" Избранное")}</button>
    </div>}
    {state?.started && <small>{t("Начало чтения")} · <time dateTime={dateTime(state.started)}>{date(state.started)}</time></small>}
    {state?.last_read && <small>{t("Последнее чтение")} · <time dateTime={dateTime(state.last_read)}>{date(state.last_read)}</time>
      {state.percentage !== null && ' · ' + Math.round(state.percentage * 100) + '%'}{state.device && ' · ' + t(state.device)}</small>}
    {state?.finished && <small>{t("Завершено")} · <time dateTime={dateTime(state.finished)}>{date(state.finished)}</time></small>}
    <details>
      <summary>{t("Коллекции")}</summary>
      {collections.data?.length === 0 && <Link to="/collections">{t("Создать коллекцию")}</Link>}
      {collections.data?.map(entry => <label className="check" key={entry.id}>
        <input type="checkbox" checked={entry.contains} disabled={busy}
          onChange={e => change('/collections/' + entry.id + '/books/' + work.id, e.target.checked ? 'PUT' : 'DELETE')}/>
        {entry.name}
      </label>)}
    </details>
    {(error || query.error || collections.error) && <Notice error>{error || message(query.error || collections.error)}</Notice>}
  </section>;
}

function ReadingCards({items}: {items: ShelfItem[]}) {
  return <ul className="book-grid">{items.map(({book, state}) => {
    const file = book.editions.flatMap(edition => edition.files).find(part => part.id === state.last_file_id);
    const href = file && ['epub', 'pdf'].includes(file.format) ? '/read/' + file.id : '/books/' + book.id;
    return <li key={book.id}>
      <Link className="book-link" to={'/books/' + book.id}><Cover book={book}/>
        <strong>{book.title}</strong><span>{book.authors.join(', ') || t("Автор не указан")}</span></Link>
      {state.percentage !== null && <div className="reading-card-info">
        <progress aria-label={t("Прогресс: ") + book.title} value={state.percentage} max={1}/>
        <small>{Math.round(state.percentage * 100)}%{state.last_read && ' · ' + date(state.last_read)}</small>
        {state.chapter && <small>{state.chapter}</small>}
        {state.device && <small>{t(state.device)}</small>}
      </div>}
      {file && <Link className="button" to={href}>{t("Продолжить")}</Link>}
    </li>;
  })}</ul>;
}

export function Home() {
  const query = useQuery({queryKey: ['reading-home'], queryFn: () => api<Overview>('/reading/home')});
  const data = query.data;
  return <>
    <header className="heading"><h1>{t("Главная")}</h1></header>
    {query.error && <Notice error>{message(query.error)}</Notice>}
    {query.isPending && <div className="shelf-loading" role="status" aria-label={t("Загружаем библиотеку")}/>}
    {data && <>
      <section className="home-section">
        <h2>{t("Продолжить чтение")}</h2>
        {data.reading.length ? <ReadingCards items={data.reading}/> : <section className="empty">
          <BookCheck size={28} aria-hidden/><p>{t("Книги появятся здесь после начала чтения.")}</p><Link className="button" to="/">{t("Открыть каталог")}</Link>
        </section>}
      </section>
      <section className="home-section">
        <h2>{t("Недавно добавленные")}</h2>
        {data.recent.length ? <BookGrid books={data.recent}/> : <p>{t("В библиотеке пока нет книг.")}</p>}
      </section>
      <section className="home-section"><h2>{t("Чтение в цифрах")}</h2>
        <dl className="reading-statistics">
          <div><dt>{t("В процессе")}</dt><dd>{data.statistics.reading}</dd></div>
          <div><dt>{t("Прочитано")}</dt><dd>{data.statistics.finished}</dd></div>
          <div><dt>{t("Прочитано в этом месяце")}</dt><dd>{data.statistics.finished_this_month}</dd></div>
          <div><dt>{t("Активное чтение")}</dt><dd>{data.statistics.seconds < 60
            ? data.statistics.seconds + t(" сек") : Math.floor(data.statistics.seconds / 60) + t(" мин")}</dd></div>
        </dl>
        <Link to="/activity">{t("История чтения")}</Link>
      </section>
    </>}
  </>;
}

export function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const requested = Number(params.get('page'));
  const page = Number.isSafeInteger(requested) && requested > 0 ? requested : 1;
  const pageSize = 30;
  const query = useQuery({queryKey: ['reading-activity', page], queryFn: () =>
    api<Activity[]>(`/reading/sessions?limit=${pageSize + 1}&offset=${(page - 1) * pageSize}`)});
  return <>
    <h1>{t("История чтения")}</h1>
    {query.error && <Notice error>{message(query.error)}</Notice>}
    {query.isPending && <div className="shelf-loading" role="status" aria-label={t("Загружаем историю")}/>}
    {query.data?.length === 0 && page === 1 && <p>{t("Сеансы чтения появятся после открытия EPUB или PDF.")}</p>}
    <ul className="rows">{query.data?.slice(0, pageSize).map((item, index) => <li key={item.file_id + ':' + index}>
      <div><Link to={'/books/' + item.work_id}>{item.title}</Link>
        <small>{date(item.opened)} · {item.seconds < 60 ? item.seconds + t(" сек") : Math.floor(item.seconds / 60) + t(" мин")}</small></div>
      <Link className="button" to={'/read/' + item.file_id}>{t("Открыть")}</Link>
    </li>)}</ul>
    <PageButtons page={page} next={(query.data?.length ?? 0) > pageSize}
      move={next => setParams(next === 1 ? {} : {page: String(next)})}/>
  </>;
}

function PageButtons({page, next, move}: {page: number; next?: boolean; move: (page: number) => void}) {
  return page > 1 || next ? <div className="pager">
    <button disabled={page === 1} onClick={() => move(page - 1)}>{t("Назад")}</button>
    <span>{t("Страница ")}{page}</span>
    <button disabled={!next} onClick={() => move(page + 1)}>{t("Далее")}</button>
  </div> : null;
}

export function Shelf() {
  const [params, setParams] = useSearchParams();
  const value = params.get('view') || '';
  const view = Object.hasOwn(views, value) ? value as keyof typeof views : 'reading';
  const page = Math.max(1, Number(params.get('page')) || 1);
  const query = useQuery({queryKey: ['reading-shelf', view, page], queryFn: () =>
    api<{items: ShelfItem[]; has_next: boolean}>('/reading/shelf?view=' + view + '&page=' + page)});
  return <>
    <h1>{t("Мои книги")}</h1>
    <nav className="shelf-tabs" aria-label={t("Личные списки")}>
      {Object.entries(views).map(([key, title]) => <Link className="button" key={key} to={'?view=' + key}
        aria-current={key === view ? 'page' : undefined}>{t(title)}</Link>)}
    </nav>
    {query.error && <Notice error>{message(query.error)}</Notice>}
    {query.isPending && <div className="shelf-loading" role="status" aria-label={t("Загружаем книги")}/>}
    {query.data && (query.data.items.length ? <ReadingCards items={query.data.items}/> : <section className="empty">
      <p>{t("В этом списке пока нет книг.")}</p><Link className="button" to="/">{t("Посмотреть каталог")}</Link>
    </section>)}
    <PageButtons page={page} next={query.data?.has_next} move={number => setParams({view, page: String(number)})}/>
  </>;
}

export function Collections() {
  const notify = useToast();
  const client = useQueryClient();
  const list = useCollections();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <>
    <h1>{t("Коллекции")}</h1>
    <form className="filters" onSubmit={async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const name = new FormData(form).get('name');
      setBusy(true);
      setError('');
      try {
        await api('/collections', json('POST', {name}));
        form.reset();
        notify("Коллекция создана");
        await client.invalidateQueries({queryKey: ['collections']});
      } catch (e) {setError(message(e));} finally {setBusy(false);}
    }}>
      <input name="name" required maxLength={120} aria-label={t("Название коллекции")} placeholder={t("Название коллекции")}/>
      <button className="primary" disabled={busy}>{t("Создать коллекцию")}</button>
    </form>
    {(error || list.error) && <Notice error>{error || message(list.error)}</Notice>}
    {list.data?.length === 0 && <p>{t("Добавляйте книги в коллекции со страницы произведения.")}</p>}
    <ul className="rows">{list.data?.map(entry => <li key={entry.id}>
      <Link to={'/collections/' + entry.id}>{entry.name}</Link>
    </li>)}</ul>
  </>;
}

export function CollectionPage() {
  const notify = useToast();
  const {id} = useParams();
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [deleted, setDeleted] = useState(false);
  const page = Math.max(1, Number(params.get('page')) || 1);
  const route = '/collections/' + id;
  const query = useQuery({queryKey: ['collection', id, page], enabled: !deleted, queryFn: () =>
    api<Collection & {items: Work[]; has_next: boolean}>(route + '?page=' + page)});
  if (deleted) return <p>{t("Коллекция удалена. ")}<Link to="/collections">{t("Все коллекции")}</Link></p>;
  if (!query.data) return query.error ? <Notice error>{message(query.error)}</Notice> : <p>{t("Загружаем коллекцию…")}</p>;
  const collection = query.data;
  async function save(verb: string, name?: FormDataEntryValue | null) {
    setBusy(true);
    setError('');
    try {
      await api(route, json(verb, name ? {name} : undefined));
      if (verb === 'DELETE') setDeleted(true);
      else {
        notify("Коллекция переименована");
        await client.invalidateQueries({queryKey: ['collection', id]});
      }
      await client.invalidateQueries({queryKey: ['collections']});
    } catch (e) {setError(message(e));} finally {setBusy(false);}
  }
  return <>
    <Link className="back" to="/collections">{t("Все коллекции")}</Link>
    <h1>{collection.name}</h1>
    {error && <Notice error>{error}</Notice>}
    <details className="collection-settings"><summary>{t("Управление коллекцией")}</summary>
      <form className="filters" onSubmit={event => {
        event.preventDefault();
        void save('PATCH', new FormData(event.currentTarget).get('name'));
      }}>
        <input name="name" required maxLength={120} aria-label={t("Название коллекции")} defaultValue={collection.name}/>
        <button disabled={busy}>{t("Переименовать")}</button>
        <button type="button" disabled={busy} onClick={() => {
          if (confirm(t("Удалить коллекцию «{name}»?", {name: collection.name}))) void save('DELETE');
        }}>{t("Удалить коллекцию")}</button>
      </form>
    </details>
    {collection.items.length ? <BookGrid books={collection.items}/> : <p>{t("В коллекции пока нет книг.")}</p>}
    <PageButtons page={page} next={collection.has_next} move={number => setParams({page: String(number)})}/>
  </>;
}


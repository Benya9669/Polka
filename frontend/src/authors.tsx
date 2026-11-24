import {t} from './locale';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';
import {Link, Navigate, useParams, useSearchParams} from 'react-router-dom';
import {api, ApiError, json, type User, type Work} from './api';
import {BookGrid, useLibraries} from './library';
import {message, Notice} from './main';
import {useToast} from './toasts';

type Entry = {id: number; name: string; library_id: number; library: string; books: number};
type Profile = Omit<Entry, 'books'> & {aliases: {key: string; name: string}[]; books: Work[]; has_next: boolean};

export function Pagination({page, more, change}: {page: number; more?: boolean; change: (page: number) => void}) {
  if (page === 1 && !more) return null;
  return <nav className="pager" aria-label={t("Страницы")}>
    <button disabled={page === 1} onClick={() => change(page - 1)}>{t("Назад")}</button>
    <span>{t("Страница ")}{page}</span>
    <button disabled={!more} onClick={() => change(page + 1)}>{t("Дальше")}</button>
  </nav>;
}

export function AuthorsPage() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get('page')) || 1;
  const term = params.get('q') || '';
  const library = params.get('library_id') || '';
  const libs = useLibraries();
  const result = useQuery({queryKey: ['authors', term, library, page], queryFn: () => {
    const criteria = new URLSearchParams({q:term, page:String(page)});
    if (library) criteria.set('library_id', library);
    return api<{items: Entry[]; has_next: boolean}>('/authors?' + criteria);
  }});
  return <>
    <header className="heading"><h1>{t("Авторы")}</h1></header>
    <form className="filters" key={term + '|' + library} onSubmit={event => {
      event.preventDefault();
      const fields = new FormData(event.currentTarget);
      const values = new URLSearchParams();
      const query = String(fields.get('q') || '').trim();
      const selected = String(fields.get('library_id') || '');
      if (query) values.set('q', query);
      if (selected) values.set('library_id', selected);
      setParams(values);
    }}>
      <input name="q" aria-label={t("Имя или псевдоним")} placeholder={t("Имя или псевдоним")} maxLength={1000} defaultValue={term}/>
      <select name="library_id" aria-label={t("Библиотека")} defaultValue={library}>
        <option value="">{t("Все библиотеки")}</option>
        {libs.data?.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}
      </select>
      <button>{t("Найти")}</button>
    </form>
    {result.isPending && <p role="status">{t("Загружаем авторов…")}</p>}
    {result.error && <Notice error>{message(result.error)}</Notice>}
    {result.data?.items.length === 0 && <p>{t("На этой странице нет авторов.")}</p>}
    <ul className="rows">{result.data?.items.map(author => <li key={author.id}>
      <div><strong><Link to={'/authors/' + author.id}>{author.name}</Link></strong><small>{author.library}</small></div>
      <small>{t("Книг: ")}{author.books}</small>
    </li>)}</ul>
    <Pagination page={page} more={result.data?.has_next} change={next => {
      const values = new URLSearchParams(params);
      values.set('page', String(next));
      setParams(values);
    }}/>
  </>;
}

export function AuthorPage({user}: {user: User}) {
  const notify = useToast();
  const {id} = useParams();
  const [params, setParams] = useSearchParams();
  const page = Number(params.get('page')) || 1;
  const client = useQueryClient();
  const libs = useLibraries();
  const route = '/authors/' + id;
  const result = useQuery({queryKey: ['author', id, page], queryFn: () => api<Profile>(route + '?page=' + page)});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [conflict, setConflict] = useState('');
  const author = result.data;
  if (!author) return result.error ? <Notice error>{message(result.error)}</Notice> : <p role="status">{t("Открываем профиль…")}</p>;
  if (String(author.id) !== id) return <Navigate to={'/authors/' + author.id} replace/>;
  const canEdit = !user.read_only && (user.admin || libs.data?.find(lib => lib.id === author.library_id)?.owner_id === user.id);
  async function save(action: () => Promise<unknown>, text: string, alias?: string) {
    setBusy(true);
    setNotice(undefined);
    setConflict('');
    try {
      await action();
      notify(text);
      await Promise.all(['author', 'authors', 'books', 'book', 'facets'].map(key => client.invalidateQueries({queryKey: [key]})));
    } catch (e) {
      setNotice({text:message(e), error:true});
      if (alias && e instanceof ApiError && e.status === 409) setConflict(alias);
    } finally { setBusy(false); }
  }
  return <>
    <Link className="back" to="/authors">{t("Все авторы")}</Link>
    <header className="heading"><div><h1>{author.name}</h1><p>{author.library}</p></div></header>
    {notice && <Notice error={notice.error}>{notice.text}</Notice>}
    {(author.aliases.length > 0 || canEdit) && <section className="panel" aria-labelledby="aliases-heading">
      <h2 id="aliases-heading">{t("Псевдонимы и варианты имени")}</h2>
      {!!author.aliases.length && <ul className="rows">{author.aliases.map(alias => <li key={alias.key}>
        <span>{alias.name}</span>
        {canEdit && <button disabled={busy} onClick={() => save(() => api(route + '/aliases/' + alias.key, json('DELETE')),
          t("Псевдоним удалён."))}>{t("Удалить")}</button>}
      </li>)}</ul>}
      {canEdit && <form className="fields" onChange={() => setConflict('')} onSubmit={event => {
        event.preventDefault();
        const form = event.currentTarget;
        const name = String(new FormData(form).get('name')).trim();
        void save(async () => {await api(route + '/aliases', json('POST', {name})); form.reset();}, t("Псевдоним добавлен."), name);
      }}>
        <label>{t("Добавить псевдоним")}<input name="name" required maxLength={1000}/></label>
        <div className="actions">
          <button className="primary" disabled={busy}>{t("Добавить")}</button>
          {conflict && <button type="button" disabled={busy} onClick={event => {
            const form = event.currentTarget.form;
            void save(async () => {await api(route + '/aliases', json('POST', {name:conflict, merge_existing:true})); form?.reset();},
              t("Профили объединены."));
          }}>{t("Объединить с профилем «{name}»", {name: conflict})}</button>}
        </div>
      </form>}
    </section>}
    {canEdit && <details className="panel">
      <summary>{t("Изменить основное имя")}</summary>
      <form className="fields schedule-fields" key={author.name} onSubmit={event => {
        event.preventDefault();
        const name = new FormData(event.currentTarget).get('name');
        void save(() => api(route, json('PUT', {name})), t("Основное имя изменено."));
      }}>
        <label>{t("Основное имя")}<input name="name" defaultValue={author.name} maxLength={1000} required/></label>
        <button disabled={busy}>{t("Сохранить")}</button>
      </form>
    </details>}
    <h2>{t("Книги автора")}</h2>
    <BookGrid books={author.books}/>
    <Pagination page={page} more={author.has_next} change={next => setParams({page:String(next)})}/>
  </>;
}

import {currentLanguage, t} from './locale';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';
import {Link, useParams} from 'react-router-dom';
import {api, bookExtensions, json, type Edition, type Job, type Work} from './api';
import {message, Notice} from './main';
import {useToast} from './toasts';

type Layout = {template: string; active: Job | null; last_plan: number | null; job: Job | null;
  removable_snapshots: {id: number; files: number; location: string}[]};
type Plan = {id: number; template: string; files: number; job: number | null; items: {file_id: number; path: string}[]; has_next: boolean};
const filePlural = {ru: new Intl.PluralRules('ru'), en: new Intl.PluralRules('en')};
const countFiles = (count: number) => count + ' ' + t({one:'файл', few:'файла', many:'файлов',
  other:'файла', zero:'файлов', two:'файла'}[filePlural[currentLanguage()].select(count)]);

type MergePlan = {source: Work; target: Work; revision: string};

export function MergeWorks({target, onMerged}: {target: Work; onMerged: (book: Work) => Promise<void>}) {
  const [term, setTerm] = useState('');
  const [page, setPage] = useState(1);
  const [plan, setPlan] = useState<MergePlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const endpoint = '/books/' + target.id + '/merge';
  const search = useQuery({queryKey: ['books', 'merge', target.library_id, term, page], enabled: Boolean(term),
    queryFn: () => api<{items: Work[]; has_next: boolean}>('/books?' + new URLSearchParams({q:term,
      library_id:String(target.library_id), sort:'title', page:String(page), limit:'12'}))});
  const candidates = search.data?.items.filter(item => item.id !== target.id);
  async function run(action: () => Promise<void>) {
    setError('');
    setBusy(true);
    try { await action(); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-labelledby="merge-heading">
    <h2 id="merge-heading">{t("Объединить карточки")}</h2>
    {!plan && <>
      <p>{t("Найдите вторую карточку в этой библиотеке. Текущая карточка останется основной.")}</p>
      <form className="fields" onSubmit={event => {
        event.preventDefault();
        setError('');
        setPage(1);
        setTerm(String(new FormData(event.currentTarget).get('query') || '').trim());
      }}>
        <label>{t("Найти вторую карточку")}<input name="query" required maxLength={300} defaultValue={term}
          placeholder={t("Название, автор или ISBN")}/></label>
        <button disabled={busy}>{t("Найти")}</button>
      </form>
      {term && search.isPending && <p role="status">{t("Ищем карточки…")}</p>}
      {candidates?.length === 0 && <p>{t("На этой странице нет подходящих карточек.")}</p>}
      {!!candidates?.length && <ul className="rows">{candidates.map(candidate => <li key={candidate.id}>
        <div><Link to={'/books/' + candidate.id} target="_blank" rel="noreferrer">{candidate.title}</Link>
          <small>{[candidate.authors.join(', '), candidate.series, candidate.series_index,
            candidate.editions.map(edition => edition.language).filter(Boolean).join(', ')].filter(Boolean).join(' · ')}</small>
          <small>{candidate.editions.flatMap(edition => edition.files).map(file => file.name).join(', ')}</small></div>
        <button disabled={busy} aria-label={t("Предпросмотр для ") + candidate.title} onClick={() => run(async () => {
          setPlan(await api<MergePlan>(endpoint + '/preview', json('POST', {source_id:candidate.id})));
        })}>{t("Предпросмотр")}</button>
      </li>)}</ul>}
      {(page > 1 || search.data?.has_next) && <div className="pager">
        <button disabled={busy || page === 1} onClick={() => setPage(page - 1)}>{t("Назад")}</button>
        <button disabled={busy || !search.data?.has_next} onClick={() => setPage(page + 1)}>{t("Дальше")}</button>
      </div>}
    </>}
    {plan && <>
      <h3>{t("Из «{source}» в «{target}»", {source: plan.source.title, target: plan.target.title})}</h3>
      <p>{t("Каждое издание сохранится отдельно. Файлы, позиции чтения, заметки и коллекции будут доступны через основную карточку. Подтверждение окончательно объединяет карточки.")}</p>
      <ul className="rows">{plan.source.editions.map(edition => <li key={edition.id}><div>
        <strong>{[edition.language, edition.publisher, edition.year, edition.isbn].filter(Boolean).join(' · ') || t("Издание")}</strong>
        {edition.files.map(file => <small key={file.id}>{file.name} · {file.format.toUpperCase()}</small>)}
      </div></li>)}</ul>
      <div className="actions">
        <button disabled={busy} onClick={() => {setPlan(undefined); setError('');}}>{t("Назад к поиску")}</button>
        <button className="primary" disabled={busy} onClick={() => run(async () => {
          await onMerged(await api<Work>(endpoint, json('POST', {source_id:plan.source.id, revision:plan.revision})));
        })}>{t("Подтвердить объединение")}</button>
      </div>
    </>}
    {(error || search.error) && <Notice error>{error || message(search.error)}</Notice>}
  </section>;
}

export function LayoutPage() {
  const {id} = useParams();
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const client = useQueryClient();
  const endpoint = '/libraries/' + id + '/layout';
  const query = useQuery({queryKey: ['layout', id], queryFn: () => api<Layout>(endpoint), refetchInterval: query =>
    ['queued','running'].includes(query.state.data?.job?.status || '') ? 2000 : false});
  const current = query.data;
  const last = current?.last_plan;
  const plan = useQuery({queryKey: ['layout-plan', id, last, page], enabled: Boolean(last),
    queryFn: () => api<Plan>(endpoint + '/plans/' + last + '?page=' + page)});
  async function run(action: () => Promise<unknown>) {
    setError('');
    setBusy(true);
    try {
      await action();
      await Promise.all([query.refetch(), client.invalidateQueries({queryKey: ['layout-plan', id]}),
        client.invalidateQueries({queryKey: ['libraries']})]);
    } catch (e) {setError(message(e));} finally {setBusy(false);}
  }
  return <>
    <Link className="back" to="/libraries">{t("Библиотеки")}</Link>
    <h1>{t("Раскладка файлов")}</h1>
    {(error || query.error || plan.error) && <Notice error>{error || message(query.error || plan.error)}</Notice>}
    {current && <>
      <form className="panel fields" key={current.template} onSubmit={event => {
        event.preventDefault();
        const template = new FormData(event.currentTarget).get('template');
        void run(() => api(endpoint, json('PUT', {template})));
      }}>
        <label>{t("Шаблон пути")}<input name="template" required maxLength={280} defaultValue={current.template}/></label>
        <p>{t("Доступные поля ")}<code>{'{author} {title} {series} {subseries} {series_index} {year} {language} {format}'}</code></p>
        <div className="actions">
          <button disabled={busy}>{t("Сохранить шаблон")}</button>
          <button type="button" className="primary" disabled={busy} onClick={() => {
            setPage(1);
            void run(() => api(endpoint + '/plans', json('POST')));
          }}>{t("Построить план по сохранённому шаблону")}</button>
        </div>
      </form>
      {current.active && <section className="panel">
        <h2>{t("Активная раскладка")}</h2>
        {current.active.result?.files !== undefined && <p>{countFiles(current.active.result.files)}</p>}
        <p>{t("Состав файлов зафиксирован при подтверждении плана.")}</p>
        <p><code>{current.active.result?.location}</code></p>
        <div className="actions">
          <a className="button" href={'/api/v1' + endpoint + '/manifest'}>{t("Скачать манифест")}</a>
          <button disabled={busy} onClick={() => {
            if (confirm(t("Вернуться к предыдущей раскладке?"))) {
              void run(() => api(endpoint + '/undo?expected=' + current.active!.id, json('POST')));
            }
          }}>{t("Отменить последнюю раскладку")}</button>
        </div>
      </section>}
      {plan.data && <section className="panel">
        <h2>{t("Последний план")}</h2>
        <p>{countFiles(plan.data.files)} · <code>{plan.data.template}</code></p>
        {current.job?.error && <Notice error>{t(current.job.error)}</Notice>}
        {['queued','running'].includes(current.job?.status || '') && <p role="status">{t("Раскладка поставлена в очередь")}</p>}
        <ul className="rows">{plan.data.items.map(item => <li key={item.file_id}>{item.path}</li>)}</ul>
        {(page > 1 || plan.data.has_next) && <div className="pager">
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>{t("Назад")}</button>
          <span>{t("Страница ")}{page}</span>
          <button disabled={!plan.data.has_next} onClick={() => setPage(page + 1)}>{t("Далее")}</button>
        </div>}
        <div className="actions">
          {(!current.job || current.job.status === 'error') && <button className="primary" disabled={busy}
            onClick={() => run(() => api(endpoint + '/plans/' + plan.data!.id + '/apply', json('POST')))}>{t("Подтвердить раскладку")}</button>}
          <Link to="/downloads">{t("Журнал операций")}</Link>
        </div>
      </section>}
      {current.removable_snapshots.length > 0 && <section className="panel">
        <h2>{t("Снимки после отмены раскладки")}</h2>
        <p>{t("Удаление освобождает место, сохраняя исходные книги и всю доступную историю отката.")}</p>
        <ul className="rows">{current.removable_snapshots.map(snapshot => <li key={snapshot.id}>
          <div>{countFiles(snapshot.files)} · <code>{snapshot.location}</code></div>
          <button disabled={busy} onClick={() => {
            if (confirm(t("Удалить файлы из снимка раскладки?") + '\n' + snapshot.location)) {
              void run(() => api(endpoint + '/snapshots/' + snapshot.id, json('DELETE')));
            }
          }}>{t("Удалить снимок")}</button>
        </li>)}</ul>
      </section>}
    </>}
  </>;
}

export function TrashPage() {
  const notify = useToast();
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState<number>();
  const [error, setError] = useState('');
  const client = useQueryClient();
  const query = useQuery({queryKey: ['trash', page], queryFn: () =>
    api<{items: Work[]; has_next: boolean}>('/books/trash?page=' + page)});
  return <>
    <Link className="back" to="/libraries">{t("Библиотеки")}</Link>
    <h1>{t("Корзина")}</h1>
    {(error || query.error) && <Notice error>{error || message(query.error)}</Notice>}
    {query.data?.items.length === 0 && <p>{t("Корзина пуста.")}</p>}
    <ul className="rows">{query.data?.items.map(book => <li key={book.id}>
      <div><strong>{book.title}</strong><small>{book.authors.join(', ')}</small></div>
      <button disabled={busy === book.id} onClick={async () => {
        setBusy(book.id);
        setError('');
        try {
          await api('/books/' + book.id + '/restore', json('POST'));
          notify("Книга восстановлена");
          await Promise.all([query.refetch(), client.invalidateQueries({queryKey: ['books']}),
            client.invalidateQueries({queryKey: ['reading-home']}), client.invalidateQueries({queryKey: ['facets']})]);
        } catch (e) {setError(message(e));} finally {setBusy(undefined);}
      }}>{t("Восстановить")}</button>
    </li>)}</ul>
    {(page > 1 || query.data?.has_next) && <div className="pager">
      <button disabled={page <= 1} onClick={() => setPage(page - 1)}>{t("Назад")}</button>
      <span>{t("Страница ")}{page}</span>
      <button disabled={!query.data?.has_next} onClick={() => setPage(page + 1)}>{t("Далее")}</button>
    </div>}
  </>;
}

export function Editions({editions, writable, busy, save, upload}: {
  editions: Edition[]; writable: boolean; busy: boolean; save: (id: number, data: object) => Promise<void>;
  upload?: (data: FormData, editionId?: string) => Promise<void>;
}) {
  return <section>
    <h2>{t("Издания")}</h2>
    <ul className="rows">{editions.map(edition => <li key={edition.id}>
      <div>
        <strong>{[edition.publisher, edition.year].filter(Boolean).join(' · ') || t("Издание")}</strong>
        <small>{[edition.language, edition.isbn && 'ISBN ' + edition.isbn].filter(Boolean).join(' · ')}</small>
        <small>{edition.metadata_source === 'manual' ? t("Метаданные изменены вручную")
          : edition.metadata_source === 'filename' ? t("Название по имени файла") : t("Метаданные из файла")}</small>
        {writable && <details>
          <summary>{t("Редактировать издание")}</summary>
          <form className="fields" onSubmit={event => {
            event.preventDefault();
            void save(edition.id, Object.fromEntries(new FormData(event.currentTarget)));
          }}>
            <label>{t("Язык")}<input name="language" maxLength={40} defaultValue={edition.language}/></label>
            <label>{t("Издательство")}<input name="publisher" maxLength={500} defaultValue={edition.publisher}/></label>
            <label>{t("Год")}<input name="year" maxLength={25} defaultValue={edition.year}/></label>
            <label>ISBN<input name="isbn" maxLength={60} defaultValue={edition.isbn}/></label>
            <label>{t("Аннотация")}<textarea name="summary" maxLength={20000} defaultValue={edition.summary}/></label>
            <button disabled={busy}>{t("Сохранить издание")}</button>
          </form>
        </details>}
      </div>
    </li>)}</ul>
    {upload && <details className="panel"><summary>{t("Добавить издание или формат")}</summary>
      <form className="fields" onSubmit={event => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        const edition = String(data.get('edition_id') || '');
        data.delete('edition_id');
        void upload(data, edition || undefined);
      }}>
        <label>{t("Издание для файла")}<select name="edition_id">
          <option value="">{t("Добавить отдельное издание")}</option>
          {editions.map((edition, index) => <option key={edition.id} value={edition.id}>
            {[t("Издание ") + String(index+1), edition.language, edition.publisher, edition.year, edition.isbn].filter(Boolean).join(' · ')}
          </option>)}
        </select></label>
        <label>{t("Файл издания")}<input type="file" name="file" required accept={bookExtensions}/></label>
        <button disabled={busy}>{t("Прикрепить файл")}</button>
      </form>
    </details>}
  </section>;
}


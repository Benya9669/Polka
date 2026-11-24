import {currentLanguage, t} from './locale';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { BookOpen, ChevronLeft, ChevronRight, FileUp, Search } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, bookExtensions, conversionTargets, json, type BookFile, type Job, type Library, type ScanSettings, type User, type Work } from './api';
import { message, Notice } from './main';
import { ReadingControls } from './personal';
import { AccountSettings } from './account';
import { Editions, MergeWorks } from './management';
import { SeriesEditor } from './series';
import {useToast} from './toasts';

const listLines = (value: FormDataEntryValue | null) =>
  String(value || '').split('\n').map(entry => entry.trim()).filter(Boolean);
const formatList = (value: FormDataEntryValue | null) =>
  Array.from(new Set(String(value || '').toLowerCase().split(/[\s,]+/).filter(Boolean))).sort();

export function useLibraries() {
  return useQuery({queryKey: ['libraries'], queryFn: () => api<Library[]>('/libraries')});
}

export function Cover({book}: {book: Work}) {
  const cover = book.editions.find(edition => edition.cover)?.cover;
  if (cover) return <img className="cover" src={cover} alt="" loading="lazy"/>;
  return <div className="cover printed" data-color={book.id % 4} aria-hidden="true">
    <span>{book.authors.join(', ')}</span><strong>{book.title}</strong><BookOpen size={18}/>
  </div>;
}

export function BookGrid({books, selected, toggle}: {books: Work[]; selected?: number[]; toggle?: (id: number) => void}) {
  return <ul className="book-grid">{books.map(book => <li key={book.id}>
    <Link className="book-link" to={'/books/' + book.id}>
      <Cover book={book}/>
      <strong>{book.title}</strong>
      <span>{book.authors.join(', ') || t("Автор не указан")}</span>
    </Link>
    {toggle && <label className="check"><input type="checkbox" checked={selected?.includes(book.id) || false}
      aria-label={t("Выбрать ") + book.title} onChange={() => toggle(book.id)}/>{t(" Выбрать")}</label>}
  </li>)}</ul>;
}

export function Catalog({user}: {user: User}) {
  const notify = useToast();
  const [params, setParams] = useSearchParams();
  const search = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const term = params.get('q') || '';
  const library = params.get('library_id') || '';
  const page = Math.max(1, Number(params.get('page')) || 1);
  const libraries = useLibraries();
  const filters = {author: t("Автор"), series: t("Серия"), genre: t("Жанр"), tag: t("Метка")} as const;
  const facets = useQuery({queryKey: ['facets', library], queryFn: () => api<{
    authors: string[]; series: string[]; genres: string[]; tags: string[];
  }>('/books/facets' + (library ? '?library_id=' + library : ''))});
  const values = {author: facets.data?.authors, series: facets.data?.series, genre: facets.data?.genres, tag: facets.data?.tags};
  const result = useQuery({queryKey: ['books', params.toString()],
    queryFn: () => api<{items: Work[]; has_next: boolean}>('/books?' + params.toString())});
  const editable = !user.read_only && (user.admin || Boolean(library &&
    libraries.data?.some(entry => String(entry.id) === library && entry.owner_id === user.id)));
  useEffect(() => {
    function focusSearch(event: KeyboardEvent) {
      if (event.defaultPrevented || event.isComposing || event.altKey || event.shiftKey ||
          !(event.ctrlKey || event.metaKey) || event.code !== 'KeyK') return;
      event.preventDefault();
      search.current?.focus();
      search.current?.select();
    }
    window.addEventListener('keydown', focusSearch);
    return () => window.removeEventListener('keydown', focusSearch);
  }, []);
  return <>
    <header className="heading"><div><h1>{t("Все книги")}</h1><p>{t("Ваш каталог электронных книг")}</p></div>
      <div className="actions">
        {editable && <button aria-pressed={selecting} onClick={() => {setSelecting(!selecting); setSelected([]);}}>
          {selecting ? t("Отменить выбор") : t("Выбрать книги")}</button>}
        {!user.read_only && <Link className="button primary" to="/libraries"><FileUp size={18}/>{t(" Добавить книги")}</Link>}
      </div>
    </header>
    <form className="filters" onSubmit={event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const next = new URLSearchParams();
      for (const [key, raw] of data) {
        const value = String(raw).trim();
        if (value) next.set(key, value);
      }
      setParams(next);
    }}>
      <label className="search"><Search size={18} aria-hidden/><input ref={search} key={term} type="search" name="q" defaultValue={term}
        aria-label={t("Поиск по метаданным")} aria-keyshortcuts="Control+K Meta+K" placeholder={t("Название, автор или ISBN")}/></label>
      <button>{t("Найти")}</button>
      <select aria-label={t("Библиотека")} name="library_id" value={library} onChange={event => {
        setSelected([]);
        const next = new URLSearchParams(params);
        next.delete('page');
        for (const key of Object.keys(filters)) next.delete(key);
        if (event.target.value) next.set('library_id', event.target.value);
        else next.delete('library_id');
        setParams(next);
      }}>
        <option value="">{t("Все библиотеки")}</option>{libraries.data?.map(lib => <option value={lib.id} key={lib.id}>{lib.name}</option>)}
      </select>
      <select key={params.get('sort')} name="sort" aria-label={t("Сортировка")} defaultValue={params.get('sort') || ''}>
        <option value="">{t("Сначала новые")}</option><option value="title">{t("По названию")}</option><option value="oldest">{t("Сначала старые")}</option>
      </select>
      {(Object.entries(filters) as [keyof typeof filters, string][]).map(([key, label]) => {
        const selectedValue = params.get(key) || '';
        const entries = Array.from(new Set([selectedValue, ...(values[key] || [])])).filter(Boolean);
        return <select key={key + ':' + selectedValue} name={key} aria-label={label} defaultValue={selectedValue}>
          <option value="">{label}</option>{entries.map(option => <option key={option}>{option}</option>)}</select>;
      })}
    </form>
    {notice && <Notice error={notice.error}>{notice.text}</Notice>}
    {selecting && editable && <form className="panel fields" onSubmit={async event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const add_tags = listLines(data.get('add_tags'));
      const remove_tags = listLines(data.get('remove_tags'));
      const series = data.has('change_series') ? String(data.get('series')).trim() : undefined;
      const genres = data.has('change_genres') ? listLines(data.get('genres')) : undefined;
      if (!add_tags.length && !remove_tags.length && series === undefined && genres === undefined) {
        setNotice({text:t("Укажите, какие поля изменить."), error:true});
        return;
      }
      setBusy(true);
      try {
        const response = await api<{updated: number}>('/books/batch', json('PATCH', {ids:selected, add_tags, remove_tags, series, genres}));
        setNotice(undefined);
        notify(t("Изменено книг: ") + response.updated);
        setSelecting(false);
        setSelected([]);
        await Promise.all([queryClient.invalidateQueries({queryKey: ['books']}), queryClient.invalidateQueries({queryKey: ['facets']}),
          queryClient.invalidateQueries({queryKey: ['book']})]);
      } catch (e) {setNotice({error:true, text:message(e)});} finally {setBusy(false);}
    }}>
      <h2>{t("Изменить выбранные книги")}</h2>
      <p>{t("Выбрано ")}{selected.length}{t(" из 100")}</p>
      <div className="split">
        <label>{t("Добавить метки по одной на строке")}<textarea name="add_tags"/></label>
        <label>{t("Удалить метки по одной на строке")}<textarea name="remove_tags"/></label>
        <div className="fields"><label className="check"><input type="checkbox" name="change_series"/>{t(" Изменить серию")}</label>
          <label>{t("Серия для выбранных книг")}<input name="series" maxLength={500}/></label></div>
        <div className="fields"><label className="check"><input type="checkbox" name="change_genres"/>{t(" Заменить жанры")}</label>
          <label>{t("Жанры по одному на строке")}<textarea name="genres"/></label></div>
      </div>
      <button className="primary" disabled={!selected.length || busy}>{t("Применить изменения")}</button>
    </form>}
    {result.isPending ? <div>
      <p role="status">{t("Загружаем каталог…")}</p>
      <ul className="book-grid" aria-hidden="true">{Array.from({length: 6}, (_, index) => <li key={index}>
        <div className="cover"/><div className="book-skeleton-title"/>
      </li>)}</ul>
    </div> : result.error ? <Notice error>{message(result.error)}</Notice>
      : result.data.items.length ? <BookGrid books={result.data.items} selected={selected} toggle={selecting && editable ? id =>
        setSelected(previous => previous.includes(id) ? previous.filter(item => item !== id) : [...previous, id].slice(0, 100)) : undefined}/> : <section className="empty">
        <BookOpen size={32} aria-hidden/><h2>{term ? t("Книги не найдены") : t("Здесь появятся книги")}</h2>
        <p>{term ? t("Попробуйте изменить запрос или выбрать другую библиотеку.") : t("Загрузите файл или подключите папку с книгами.")}</p>
        {!term && <Link to="/libraries" className="button">{t("Открыть библиотеки")}</Link>}
      </section>}
    {(page > 1 || result.data?.has_next) && <div className="pager">
      <button disabled={page === 1} onClick={() => setParams({...Object.fromEntries(params), page: String(page - 1)})}><ChevronLeft size={16}/>{t(" Назад")}</button>
      <span>{t("Страница ")}{page}</span>
      <button disabled={!result.data?.has_next} onClick={() => setParams({...Object.fromEntries(params), page: String(page + 1)})}>{t("Далее ")}<ChevronRight size={16}/></button>
    </div>}
  </>;
}

export function WorkPage({user}: {user: User}) {
  const notify = useToast();
  const {id} = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const book = useQuery({queryKey: ['book', id], queryFn: () => api<Work>('/books/' + id)});
  const libs = useLibraries();
  const [editing, setEditing] = useState(false);
  const [merging, setMerging] = useState(false);
  const [binding, setBinding] = useState<number>();
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [busy, setBusy] = useState(false);
  if (!book.data) return book.error ? <Notice error>{message(book.error)}</Notice> : <p>{t("Открываем книгу…")}</p>;
  const work = book.data;
  if (String(work.id) !== id) return <Navigate to={'/books/' + work.id} replace/>;
  const library = libs.data?.find(l => l.id === work.library_id);
  const own = !user.read_only && (user.admin || library?.owner_id === user.id);
  const files = work.editions.flatMap(e => e.files);
  async function perform(action: () => Promise<unknown>, text: string) {
    setBusy(true);
    setNotice(undefined);
    try {
      await action();
      notify(text);
      await client.invalidateQueries({queryKey: ['book', id]});
      await client.invalidateQueries({queryKey: ['books']});
      await client.invalidateQueries({queryKey: ['facets']});
    } catch (e) {
      setNotice({text: message(e), error: true});
    } finally {
      setBusy(false);
    }
  }
  return <>
    <Link className="back" to="/"><ChevronLeft size={16}/>{t(" Все книги")}</Link>
    <section className="book-detail"><Cover book={work}/><div>
      <h1>{work.title}</h1><p className="byline">{work.author_credits.map((credit, index) => <span key={index}>
        {index > 0 && ', '}{credit.id ? <Link to={'/authors/' + credit.id} title={credit.name}>{credit.credited_as}</Link> : credit.credited_as}
      </span>)}</p>
      {work.series_memberships.map(entry => <p key={entry.id}>
        <Link to={'/series/' + entry.id}>{entry.path.join(' / ')}</Link>{entry.position && t(" · Том ") + entry.position}</p>)}
      {!work.series_memberships.length && work.series_index && <p>{t("Том ")}{work.series_index}</p>}
      {work.genres.length > 0 && <p>{work.genres.join(' · ')}</p>}
      {work.tags.length > 0 && <p>{work.tags.map(tag => <Link key={tag} className="book-tag" to={'/?tag=' + encodeURIComponent(tag)}>{tag}</Link>)}</p>}
      {Object.keys(work.custom_fields).length > 0 && <dl>{Object.entries(work.custom_fields).map(([key, value]) =>
        <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>}
      <ReadingControls key={work.id} work={work}/>
      {work.editions.find(e => e.summary)?.summary && <p className="summary">{work.editions.find(e => e.summary)?.summary}</p>}
      {own && <div className="actions">
        <button onClick={() => setEditing(!editing)}>{editing ? t("Закрыть редактор") : t("Редактировать метаданные")}</button>
        <button aria-expanded={merging} onClick={() => setMerging(!merging)}>{merging ? t("Закрыть объединение") : t("Объединить карточки")}</button>
        {library?.mode === 'managed' && <button disabled={busy} onClick={() => {
          if (!confirm(t("Переместить «{title}» в корзину?", {title: work.title}))) return;
          void api('/books/' + id + '/trash', json('POST')).then(async () => {
            await Promise.all([client.invalidateQueries({queryKey: ['trash']}), client.invalidateQueries({queryKey: ['books']}),
              client.invalidateQueries({queryKey: ['facets']}), client.invalidateQueries({queryKey: ['reading-home']})]);
            notify("Книга перемещена в корзину");
            navigate('/trash');
          }).catch(e => setNotice({text: message(e), error:true}));
        }}>{t("В корзину")}</button>}
      </div>}
    </div></section>
    {notice && <Notice error={notice.error}>{notice.text}</Notice>}
    {own && merging && <MergeWorks key={work.id} target={work} onMerged={async merged => {
      client.setQueryData(['book', id], merged);
      setMerging(false);
      setNotice(undefined);
      notify("Карточки объединены.");
      await Promise.all(['book', 'books', 'facets', 'reading-state', 'reading-home', 'reading-shelf',
        'collection', 'collections', 'jobs'].map(key => client.invalidateQueries({queryKey: [key]})));
    }}/>}
    {editing && <form className="panel fields" onSubmit={event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      const customFields: [string, string][] = [];
      for (const line of listLines(values.custom_fields)) {
        const separator = line.indexOf('=');
        if (separator < 1 || !line.slice(0, separator).trim()) {
          setNotice({error:true, text:t("Для дополнительного поля используйте запись «название=значение».")});
          return;
        }
        customFields.push([line.slice(0, separator).trim(), line.slice(separator + 1).trim()]);
      }
      const custom_fields = Object.fromEntries(customFields);
      void perform(async () => {
        await api('/books/' + id, json('PUT', {...values, authors: listLines(values.authors), genres: listLines(values.genres),
          tags: listLines(values.tags), custom_fields, series_memberships: JSON.parse(String(values.series_memberships))}));
        setEditing(false);
      }, t("Метаданные сохранены"));
    }}>
      <label>{t("Название")}<input name="title" defaultValue={work.title} required/></label>
      <label>{t("Авторы с новой строки")}<textarea name="authors" defaultValue={work.authors.join('\n')}/></label>
      <SeriesEditor initial={work.series_memberships} initialPosition={work.series_memberships.length ? '' : work.series_index}/>
      <label>{t("Жанры с новой строки")}<textarea name="genres" defaultValue={work.genres.join('\n')}/></label>
      <label>{t("Метки с новой строки")}<textarea name="tags" defaultValue={work.tags.join('\n')}/></label>
      <label>{t("Дополнительные поля, по одному «название=значение»")}<textarea name="custom_fields"
        defaultValue={Object.entries(work.custom_fields).map(([key, value]) => key + '=' + value).join('\n')}/></label>
      <button className="primary" disabled={busy}>{t("Сохранить")}</button>
    </form>}
    <Editions editions={work.editions} writable={own} busy={busy} save={(edition, data) =>
      perform(() => api('/editions/' + edition, json('PATCH', data)), t("Метаданные издания сохранены"))}
      upload={own && library?.mode === 'managed' ? (data, editionId) => perform(() =>
        api('/books/' + id + '/editions/upload' + (editionId ? '?edition_id=' + editionId : ''),
          {method:'POST', body:data}), t("Файл прикреплён к произведению")) : undefined}/>
    <h2>{t("Файлы книги")}</h2>
    <ul className="rows">{files.map(file => <li key={file.id}>
      <div><strong>{file.format.toUpperCase()}</strong><small>{file.name} · {formatBytes(file.size)}{file.derived_from ? t(" · Конвертирован") : ''}</small></div>
      <div className="actions">
        {['epub','pdf'].includes(file.format) && <Link className="button primary" to={'/read/' + file.id}>{t("Читать")}</Link>}
        {own && Object.hasOwn(conversionTargets, file.format) && !files.some(f => f.derived_from === file.id) &&
          <button disabled={busy} onClick={() => perform(() => api('/conversions/' + file.id, json('POST')), t("Конвертация добавлена в очередь"))}>{t("Преобразовать в ")}{conversionTargets[file.format]}</button>}
        <Link className="button" to={'/devices/sony?file=' + file.id}>{t("Для Sony")}</Link>
        <button disabled={busy} aria-expanded={binding === file.id} onClick={() => setBinding(binding === file.id ? undefined : file.id)}>{t("Связать с KOReader")}</button>
        <a className="button" href={'/api/v1/files/' + file.id}>{t("Скачать")}</a>
        {binding === file.id && <form className="filters" onSubmit={event => {
          event.preventDefault();
          const document = new FormData(event.currentTarget).get('document');
          void perform(async () => {
            await api('/devices/koreader/documents', json('PUT', {document, file_id: file.id}));
            setBinding(undefined);
          }, t("Документ KOReader связан с файлом"));
        }}><input name="document" required pattern="[a-fA-F0-9]{32}" aria-label={t("Идентификатор документа KOReader")} placeholder={t("Идентификатор из настроек")}/>
        <button type="button" disabled={busy} onClick={async event => {
          const input = event.currentTarget.form?.elements.namedItem('document');
          setBusy(true);
          setNotice(undefined);
          try {
            const match = await api<{document: string | null; linked_file_id: number | null}>(
              '/devices/koreader/files/' + file.id + '/match');
            if (!match.document) throw new Error(t("Для отпечатка файла пока нет прогресса KOReader"));
            if (match.linked_file_id && match.linked_file_id !== file.id) {
              throw new Error(t("Этот документ KOReader уже связан с другим файлом"));
            }
            if (input instanceof HTMLInputElement && input.isConnected) {
              input.value = match.document;
              input.focus();
            }
          } catch (e) {
            setNotice({text: message(e), error:true});
          } finally {
            setBusy(false);
          }
        }}>{t("Найти по отпечатку файла")}</button>
        <button className="primary" disabled={busy}>{t("Связать")}</button></form>}
      </div>
    </li>)}</ul>
  </>;
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return bytes + t(" Б");
  return bytes < 1024 * 1024 ? (bytes / 1024).toFixed(0) + t(" КБ") : (bytes / 1024 / 1024).toFixed(1) + t(" МБ");
}

export function Libraries({user}: {user: User}) {
  const notify = useToast();
  const libraries = useLibraries();
  const client = useQueryClient();
  const [newMode, setMode] = useState('managed');
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Library>();
  async function write(fn: () => Promise<void>) {
    setBusy(true);
    setNotice(undefined);
    try {
      await fn();
      await Promise.all([client.invalidateQueries({queryKey: ['libraries']}), client.invalidateQueries({queryKey: ['books']})]);
    } catch (e) {
      setNotice({text: message(e), error: true});
    } finally {
      setBusy(false);
    }
  }
  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    void write(async () => {
      await api('/libraries', json('POST', {...Object.fromEntries(data), shared: data.has('shared'), mode: newMode}));
      form.reset();
      setMode('managed');
      notify("Библиотека создана");
    });
  }
  function edit(event: FormEvent<HTMLFormElement>, original: Library) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const name = String(values.get('name') || '').trim();
    const description = String(values.get('description') || '');
    const shared = values.has('shared');
    const formats = formatList(values.get('formats'));
    const updates: Partial<Pick<Library, 'name' | 'description' | 'shared' | 'formats'>> = {};
    if (name !== original.name) updates.name = name;
    if (description !== original.description) updates.description = description;
    if (user.admin && shared !== original.shared) updates.shared = shared;
    if (formats.join(',') !== (original.formats || []).join(',')) updates.formats = formats;
    if (!Object.keys(updates).length) {
      setEditing(undefined);
      return;
    }
    void write(async () => {
      await api('/libraries/' + original.id, json('PATCH', updates));
      setEditing(undefined);
      notify("Настройки библиотеки сохранены");
    });
  }
  function upload(event: FormEvent<HTMLFormElement>, id: number) {
    event.preventDefault();
    const form = event.currentTarget;
    const files = new FormData(form).getAll('file');
    void write(async () => {
      let added = 0, duplicates = 0;
      const failures = [];
      for (const file of files) {
        const data = new FormData();
        data.set('file', file);
        try {
          const result = await api<{duplicate: boolean}>('/libraries/' + id + '/upload', {method: 'POST', body: data});
          if (result.duplicate) duplicates++; else added++;
        } catch (e) {
          failures.push((file instanceof File ? file.name + ' — ' : '') + message(e));
        }
      }
      form.reset();
      const counts = [t("Добавлено ") + added, t("Дубликатов ") + duplicates];
      if (failures.length) setNotice({error: true, text: [...counts, ...failures].join(' · ')});
      else notify(counts.join(' · '));
    });
  }
  return <>
    <header className="heading"><div><h1>{t("Библиотеки")}</h1><p>{t("Управляйте коллекциями и подключёнными папками")}</p></div>
      {!user.read_only && <Link className="button" to="/trash">{t("Корзина")}</Link>}</header>
    {notice && <Notice error={notice.error}>{notice.text}</Notice>}
    <div className="split">
      <div>
        {libraries.isPending && <p>{t("Загружаем библиотеки…")}</p>}
        {libraries.error && <Notice error>{message(libraries.error)}</Notice>}
        {libraries.data?.map(lib => {
          const canWrite = !user.read_only && (user.admin || lib.owner_id === user.id);
          return <section className="panel library-panel" key={lib.id}>
            <h2><Link to={'/?library_id=' + lib.id}>{lib.name}</Link></h2>
            <p>{lib.mode === 'indexed' ? t("Индексируемая · /inbox/") + lib.folder : t("Управляемая")}{lib.shared && t(" · Общий доступ")}</p>
            {lib.description && <p>{lib.description}</p>}
            {canWrite && <div className="scan">
              <button type="button" aria-expanded={editing?.id === lib.id} disabled={busy}
                onClick={() => setEditing(editing?.id === lib.id ? undefined : lib)}>{t("Настройки библиотеки")}</button>
              {editing?.id === lib.id && <form className="fields" onSubmit={event => edit(event, editing)}>
                <label>{t("Название")}<input required maxLength={120} name="name" defaultValue={editing.name}/></label>
                <label>{t("Описание")}<textarea name="description" maxLength={2000} defaultValue={editing.description}/></label>
                <label>{t("Форматы оригиналов через запятую")}<input name="formats" defaultValue={(editing.formats || []).join(', ')}
                  placeholder={t("Все форматы, например epub, pdf")}/></label>
                {user.admin && <label className="check"><input type="checkbox" name="shared" defaultChecked={editing.shared}/>{t(" Доступна другим пользователям")}</label>}
                <button disabled={busy}>{t("Сохранить настройки библиотеки")}</button>
              </form>}
            </div>}
            {canWrite && lib.mode === 'managed' && <p><Link to={'/libraries/' + lib.id + '/layout'}>{t("Раскладка файлов")}</Link></p>}
            {canWrite && lib.mode === 'managed' && <form className="fields" onSubmit={event => upload(event, lib.id)}>
              <label>{t("Файлы книг")}<input required multiple type="file" name="file" accept={bookExtensions}/></label>
              <button disabled={busy}>{t("Загрузить файлы")}</button>
            </form>}
            {canWrite && user.admin && <form className="fields scan" onSubmit={event => {
              event.preventDefault();
              const folder = new FormData(event.currentTarget).get('folder') || '';
              void write(async () => {
                await api('/libraries/' + lib.id + '/scan', json('POST', {folder}));
                notify("Сканирование запущено. План появится в разделе «Загрузки и импорт».",
                  {to:'/downloads', label:"Открыть загрузки"});
              });
            }}>
              {lib.mode === 'managed' && <label>{t("Подкаталог в /inbox")}<input name="folder" placeholder={t("Вся папка")}/></label>}
              <button disabled={busy}>{t("Предпросмотр импорта")}</button>
            </form>}
            {canWrite && user.admin && <ScheduleSettings library={lib}/>}
          </section>;
        })}
      </div>
      {!user.read_only && <form className="panel fields" onSubmit={create}>
        <h2>{t("Новая библиотека")}</h2>
        <label>{t("Название")}<input required maxLength={120} name="name"/></label>
        <label>{t("Описание")}<textarea name="description" maxLength={2000}/></label>
        {user.admin && <>
          <label>{t("Хранение")}<select value={newMode} onChange={event => setMode(event.target.value)}>
            <option value="managed">{t("Копировать оригиналы в приложение")}</option><option value="indexed">{t("Индексировать папку только для чтения")}</option>
          </select></label>
          {newMode === 'indexed' && <label>{t("Подкаталог в /inbox")}<input name="folder" placeholder={t("Вся папка")}/></label>}
          <label className="check"><input type="checkbox" name="shared"/>{t(" Доступна другим пользователям")}</label>
        </>}
        <button className="primary" disabled={busy}>{t("Создать библиотеку")}</button>
      </form>}
    </div>
  </>;
}

const statusLabels: Record<string, string> = {queued:'В очереди', running:'В работе', done:'Готово', error:'Ошибка', cancelled:'Отменено'};
function ScheduleSettings({library}: {library: Library}) {
  const notify = useToast();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const client = useQueryClient();
  const path = '/libraries/' + library.id + '/scan-settings';
  const key = ['scan-settings', library.id];
  const settings = useQuery({queryKey: key, queryFn: () => api<ScanSettings>(path), enabled: open});
  const current = settings.data;
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const formats = formatList(data.get('formats'));
    setError('');
    setBusy(true);
    try {
      const result = await api<ScanSettings>(path, json('PUT', {mode: data.get('mode'), folder: data.get('folder') || '',
        interval_minutes: Number(data.get('interval')), watch: data.has('watch'), formats}));
      client.setQueryData(key, result);
      notify("Расписание сохранено.");
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return <details className="scan" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{t("Расписание сканирования")}</summary>
    {open && settings.isPending && <p>{t("Загружаем расписание…")}</p>}
    {(error || settings.error) && <Notice error>{error || message(settings.error)}</Notice>}
    {current && <form className="fields schedule-fields" onSubmit={save}>
      <label>{t("Режим")}<select name="mode" defaultValue={current.mode}>
        <option value="off">{t("Выключено")}</option>
        <option value="preview">{t("Готовить план импорта")}</option>
        <option value="apply">{t("Импортировать автоматически")}</option>
      </select></label>
      {library.mode === 'managed' && <label>{t("Подкаталог в /inbox для расписания")}<input name="folder" maxLength={500} defaultValue={current.folder}/></label>}
      <label>{t("Интервал в минутах")}<input name="interval" type="number" min="1" max="10080" required defaultValue={current.interval_minutes}/></label>
      <label>{t("Форматы через запятую")}<input name="formats" defaultValue={current.formats.join(', ')} placeholder={t("Все форматы, например epub, pdf")}/></label>
      <label className="check"><input type="checkbox" name="watch" defaultChecked={current.watch}/>{t(" Сканировать при изменении файлов")}</label>
      {current.next_scan && <small>{t("Следующая проверка по расписанию — ")}{new Date(current.next_scan).toLocaleString(currentLanguage())}</small>}
      <button disabled={busy}>{t("Сохранить расписание")}</button>
    </form>}
  </details>;
}

const kindLabels: Record<string, string> = {scan:'План импорта', import:'Импорт файлов', conversion:'Конвертация', download:'Загрузка OPDS',
  merge:'Объединение карточек',
  layout_plan:'План раскладки', layout:'Раскладка файлов', layout_undo:'Возврат раскладки', import_undo:'Отмена импорта',
  sony_export:'Экспорт для Sony'};

function ImportPlan({result, editable, busy, confirmPlan}: {result: NonNullable<Job['result']>; editable: boolean;
    busy: boolean; confirmPlan: (choices: Record<string, unknown>) => void}) {
  const items = result.items || [];
  const [page, setPage] = useState(0);
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const pageSize = 30;
  const choices = Object.fromEntries(Object.entries(decisions).filter(([, value]) => value).map(([path, value]) => [path,
    value === 'skip' ? {action:'skip'} : value.startsWith('w:') ? {action:'work', work_id:Number(value.slice(2))}
      : {action:'incoming', path:value.slice(2)},
  ]));
  const seen = new Set<string>();
  const newFiles = new Map<string, boolean>();
  let newWorks = 0;
  let conversions = 0;
  for (const item of editable ? items : []) {
    if (choices[item.path]?.action === 'skip' || (item.existing_duplicate ?? item.duplicate) || seen.has(item.sha256)) continue;
    seen.add(item.sha256);
    const convert = item.format === 'fb2' || item.format === 'fb2.zip';
    newFiles.set(item.path, convert);
    if (!choices[item.path]) newWorks++;
    if (convert) conversions++;
  }
  return <>
    {editable && <p>{t("Будет создано карточек: {count}", {count:newWorks})} · {t("Будет конвертировано файлов: {count}", {count:conversions})}</p>}
    {!editable && !!result.conversions && <p>{t("Конвертаций в исходном плане: {count}", {count:result.conversions})}</p>}
    <details><summary>{t("Файлы в плане (")}{items.length})</summary>
      <ul className="rows">{items.slice(page * pageSize, (page + 1) * pageSize).map(item => <li key={item.path}>
        <div>
          <strong>{item.path}</strong>
          <small>{[item.duplicate ? t("Точный дубликат") : item.format.toUpperCase(), item.language, item.isbn,
            (editable ? newFiles.get(item.path) : item.convert) ? t("Будет конвертирован") : ''].filter(Boolean).join(' · ')}</small>
          {item.matches?.map(match => <small key={match.work_id}>
            <Link to={'/books/' + match.work_id} target="_blank" rel="noreferrer">{match.title}</Link> · {match.confidence}/100 · {t(match.reason)}
          </small>)}
          {item.incoming?.map(match => <small key={match.path}>{match.path} · {match.confidence}/100 · {t(match.reason)}</small>)}
          {editable && <label>{t("Действие для файла")}<select aria-label={t("Действие для ") + item.path} disabled={busy}
            value={decisions[item.path] || ''} onChange={event => {
              const value = event.target.value;
              setDecisions(previous => ({...previous, [item.path]:value}));
            }}>
            <option value="">{item.duplicate && !newFiles.has(item.path) ? t("Обработать как дубликат") : t("Создать карточку")}</option>
            <option value="skip">{t("Пропустить")}</option>
            {!item.duplicate && item.matches?.map(match => <option key={match.work_id} value={'w:' + match.work_id}>{t("Добавить издание в «{title}»", {title: match.title})} ({match.confidence}/100)
            </option>)}
            {!item.duplicate && item.incoming?.map(match => <option key={match.path} value={'p:' + match.path}>{t("Объединить с ")}{match.path} ({match.confidence}/100)
            </option>)}
          </select></label>}
        </div>
      </li>)}</ul>
      {items.length > pageSize && <nav className="pager" aria-label={t("Страницы плана")}>
        <button disabled={page === 0} onClick={() => setPage(page - 1)}>{t("Назад")}</button>
        <span>{t("Страница ")}{page + 1}{t(" из ")}{Math.ceil(items.length / pageSize)}</span>
        <button disabled={(page + 1) * pageSize >= items.length} onClick={() => setPage(page + 1)}>{t("Дальше")}</button>
      </nav>}
    </details>
    {editable && <div className="actions scan-confirm">
      <button className="primary" disabled={busy} onClick={() => confirmPlan(choices)}>{t("Подтвердить импорт")}</button>
      {Object.keys(choices).length > 0 && <small>{t("Решений в плане: ")}{Object.keys(choices).length}</small>}
    </div>}
  </>;
}

function operationStatus(job: Job, jobs: Job[]) {
  if (job.kind === 'download') {
    if (job.status === 'running') return job.result?.stage === 'processing' ? 'Обрабатывается' : 'Скачивается';
    if (job.status === 'done') {
      const conversion = jobs.find(other => other.id === job.result?.conversion_id);
      if (conversion?.status === 'queued' || conversion?.status === 'running') return 'Конвертируется';
      return job.result?.delivery === 'file' ? 'Файл готов' : 'Добавлено в библиотеку';
    }
  }
  return statusLabels[job.status] || job.status;
}

export function Operations() {
  const jobs = useQuery({queryKey: ['jobs'], queryFn: () => api<Job[]>('/imports'), refetchInterval: 2500});
  const libraries = useLibraries();
  const client = useQueryClient();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<number>();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const desired = jobs.data?.find(job => job.kind === 'download' && job.id === Number(params.get('read')));
  const preparing = jobs.data?.find(job => job.id === desired?.result?.conversion_id);
  const desiredFile = jobs.data?.find(job => job.kind === 'download' && job.id === Number(params.get('file')));
  useEffect(() => {
    if (desired?.status === 'done' && desired.result?.read_file_id) navigate('/read/' + desired.result.read_file_id, {replace:true});
  }, [desired,navigate]);
  async function act(id: number, path: string, payload?: unknown) {
    setBusy(id);
    setError('');
    try {
      await api(path, json('POST', payload));
      await jobs.refetch();
      await client.invalidateQueries({queryKey: ['books']});
      await client.invalidateQueries({queryKey: ['facets']});
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(undefined);
    }
  }
  return <>
    <header className="heading"><div><h1>{t("Загрузки и импорт")}</h1><p>{t("Очередь, планы сканирования и результаты обработки")}</p></div></header>
    {desired && !desired.result?.read_file_id && (desired.status === 'queued' || desired.status === 'running' || preparing?.status === 'queued' || preparing?.status === 'running') &&
      <Notice>{preparing ? t("Конвертируем книгу для чтения…") : t("Читалка откроется после импорта.")}</Notice>}
    {(desiredFile?.status === 'queued' || desiredFile?.status === 'running') && <Notice>{t("Файл можно будет скачать из этой очереди после проверки.")}</Notice>}
    {(error || jobs.error) && <Notice error>{error || message(jobs.error)}</Notice>}
    {jobs.data?.length === 0 && <section className="empty"><h2>{t("Задач пока нет")}</h2><p>{t("Импортируйте папку или загрузите книгу из OPDS.")}</p></section>}
    <ul className="rows operation-list">{jobs.data?.map((job, _, operations) => <li key={job.id}>
      <div className="operation-body">
        <div className="row-heading"><strong>{job.result?.title || t(kindLabels[job.kind] || job.kind)}</strong>
          <span className={'status ' + job.status}>{t(operationStatus(job, operations))}</span></div>
        <small>{new Date(job.created).toLocaleString(currentLanguage())}</small>
        {job.error && <p className="error-text">{t(job.error)}</p>}
        {job.kind === 'download' && job.result?.source && <p>{job.result.source}{job.result.format && ' · ' + job.result.format.toUpperCase()}</p>}
        {job.kind === 'download' && job.result?.provenance && <details><summary>{job.result.delivery === 'file' ? t("История загрузки") : t("История импорта")}</summary>
          <small>{new Date(job.result.provenance.downloaded_at).toLocaleString(currentLanguage())}</small>
          {job.result.provenance.identifier && <small>{job.result.provenance.identifier}</small>}
          {job.result.provenance.metadata?.authors?.length && <small>{job.result.provenance.metadata.authors.join(', ')}</small>}
          {job.result.sha256 && <code>SHA-256 {job.result.sha256}</code>}
        </details>}
        {job.result?.expires_at && <small>{new Date(job.result.expires_at).getTime() > Date.now()
          ? t("Файл доступен до ") + new Date(job.result.expires_at).toLocaleString(currentLanguage()) : t("Срок хранения файла истёк")}</small>}
        {typeof job.result?.bytes === 'number' && <p>{formatBytes(job.result.bytes)}{job.result.total ? ' / ' + formatBytes(job.result.total) : ''}
          {job.status === 'running' && job.result.speed ? ' · ' + formatBytes(job.result.speed) + t("/с") : ''}</p>}
        {job.status === 'running' && job.result?.total && <progress value={job.result.bytes} max={job.result.total}/>}
        {job.result?.added !== undefined && <p>{t("Добавлено ")}{job.result.added}{t(" · Дубликатов ")}{job.result.duplicates}
          {!!job.result.attached && t(" · Изданий в выбранных карточках ") + job.result.attached}
          {!!job.result.skipped && t(" · Пропущено ") + job.result.skipped}</p>}
        {job.kind === 'merge' && job.result?.target_id && <p>{t("Из «{source}» в ", {source: job.result.source_title || ''})}
          <Link to={'/books/' + job.result.target_id}>{job.result.target_title}</Link></p>}
        {job.result?.undo_id && <p>{t("Импорт отменён. Новые записи отправлены в корзину.")}</p>}
        {job.result?.trashed !== undefined && <p>{t("В корзине: ")}{job.result.trashed}</p>}
        {job.result?.location && <p><code>{job.result.location}</code></p>}
        {job.result?.found !== undefined && <>
          <p>{t("Найдено ")}{job.result.found}{t(" · Дубликатов ")}{job.result.duplicates}{t(" · Совпадений ")}{job.result.candidates}</p>
          {job.result.unreadable !== undefined && <p>{t("Ошибки формата или метаданных: {count}", {count:job.result.unreadable})}</p>}
          {job.result.reused !== undefined && <p>{t("Файлов с сохранённым отпечатком: ")}{job.result.reused}</p>}
          {job.result.automatic && <p>{t("Файлов для импорта нет.")}</p>}
          <ImportPlan result={job.result} editable={job.kind === 'scan' && job.status === 'done' && !job.result.confirmed && !job.result.automatic}
            busy={busy === job.id} confirmPlan={choices => void act(job.id, '/imports/' + job.id + '/confirm', {choices})}/>
        </>}
        {!!job.result?.errors?.length && <details><summary>{t("Ошибки (")}{job.result.errors.length})</summary>
          <ul>{job.result.errors.map(item => <li key={item.path}>{item.path} · {t(item.error)}</li>)}</ul></details>}
      </div>
      <div className="actions">
        {job.kind === 'import' && job.status === 'done' && !!job.result?.work_ids?.length && !job.result.undo_id &&
          libraries.data?.find(lib => lib.id === job.library_id)?.mode === 'managed' &&
          <button disabled={busy === job.id} onClick={() => {
            if (confirm(t("Переместить новые записи этого импорта в корзину?"))) void act(job.id, '/imports/' + job.id + '/undo');
          }}>{t("Отменить импорт")}</button>}
        {job.kind.startsWith('layout') && <Link to={'/libraries/' + job.library_id + '/layout'}>{t("Открыть раскладку")}</Link>}
        {job.kind === 'sony_export' && <Link to="/devices/sony">{t("Открыть Sony PRS-600")}</Link>}
        {job.kind === 'download' && ['queued','running'].includes(job.status) && <button disabled={busy === job.id}
          onClick={() => act(job.id, '/opds/downloads/' + job.id + '/cancel')}>{t("Отменить")}</button>}
        {job.kind === 'download' && ['error','cancelled'].includes(job.status) && <button disabled={busy === job.id}
          onClick={() => act(job.id, '/opds/downloads/' + job.id + '/retry')}>{t("Повторить")}</button>}
        {job.result?.file_id && job.status === 'done' && <OperationLink fileId={job.result.file_id}/>}
        {job.kind === 'download' && job.status === 'done' && !!job.result?.file_id && <a className="button" href={'/api/v1/files/' + job.result.file_id}>{t("Скачать")}</a>}
        {job.kind === 'download' && job.status === 'done' && job.result?.expires_at && new Date(job.result.expires_at).getTime() > Date.now() &&
          <a className="button primary" href={'/api/v1/opds/downloads/' + job.id + '/file'}>{t("Скачать файл")}</a>}
        {job.kind === 'download' && job.result?.read_file_id && <Link className="button primary" to={'/read/' + job.result.read_file_id}>{t("Читать")}</Link>}
      </div>
    </li>)}</ul>
  </>;
}

function OperationLink({fileId}: {fileId: number}) {
  const item = useQuery({queryKey: ['file', fileId], queryFn: () => api<BookFile & {work_id: number}>('/files/' + fileId + '/info')});
  return item.data ? <Link className="button" to={'/books/' + item.data.work_id}>{t("К книге")}</Link> : null;
}

type HistoryItem = {file_id: number; updated: string; percentage: number | null; page?: number | null};
function HistoryRow({event}: {event: HistoryItem}) {
  const file = useQuery({queryKey: ['file', event.file_id], queryFn: () =>
    api<{title: string; format: string; work_id: number}>('/files/' + event.file_id + '/info')});
  const href = file.data && ['epub','pdf'].includes(file.data.format) ? '/read/' + event.file_id : '/books/' + file.data?.work_id;
  return file.data ? <li><div><strong>{file.data.title}</strong><small>{file.data.format.toUpperCase()}
    {event.percentage !== null ? ' · ' + Math.round(event.percentage * 100) + '%' : event.page ? t(" · Страница ") + event.page : ''}
    {' · ' + new Date(event.updated).toLocaleString(currentLanguage())}</small></div>
    <Link className="button primary" to={href}>{t("Продолжить")}</Link></li> : null;
}

export function History() {
  const result = useQuery({queryKey: ['history'], queryFn: () => api<HistoryItem[]>('/reading/history')});
  const files = result.data?.filter((event, index, all) => all.findIndex(other => other.file_id === event.file_id) === index);
  return <><h1>{t("Продолжить чтение")}</h1>
    {result.error && <Notice error>{message(result.error)}</Notice>}
    {files?.length === 0 && <section className="empty"><h2>{t("История чтения пуста")}</h2><p>{t("Откройте EPUB или PDF, и позиция появится здесь.")}</p><Link className="button" to="/">{t("Открыть каталог")}</Link></section>}
    <ul className="rows">{files?.map(event => <HistoryRow key={event.file_id} event={event}/>)}</ul>
  </>;
}

export function Settings({user, appearance, logout}: {user: User; appearance: ReactNode; logout: () => Promise<void>}) {
  const list = useQuery({queryKey: ['tokens'], queryFn: () => api<{id: string; label: string}[]>('/auth/opds-tokens')});
  const [token, setToken] = useState('');
  const [koPassword, setKoPassword] = useState('');
  const [showDocuments, setShowDocuments] = useState(false);
  const devices = useQuery({queryKey: ['ko-documents'],
    queryFn: () => api<{document: string; percentage: number | null; file_id: number | null; device: string | null}[]>('/devices/koreader/documents'),
    enabled: showDocuments, refetchInterval: showDocuments ? 5000 : false});
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [busy, setBusy] = useState(false);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setNotice(undefined);
    try { await fn(); } catch (e) { setNotice({text: message(e), error: true}); } finally { setBusy(false); }
  }
  return <>
    <header className="heading"><h1>{t("Настройки")}</h1><button disabled={busy} onClick={() => act(logout)}>{t("Выйти")}</button></header>
    <p><a href="/api/v1/reading/export">{t("Скачать мои данные чтения в JSON")}</a></p>
    {notice && <Notice error={notice.error}>{notice.text}</Notice>}
    {appearance}
    <div className="split">
      <section className="panel fields">
        <h2>{t("Доступ к OPDS")}</h2>
        <p>{t("Укажите имя пользователя ")}<strong>{user.username}</strong>{t(", а в поле пароля используйте персональный токен.")}</p>
        <label>OPDS 1.2<input readOnly value={location.origin + '/opds/v1'}/></label>
        <label>OPDS 2.0<input readOnly value={location.origin + '/opds/v2'}/></label>
        <form className="fields" onSubmit={event => {
          event.preventDefault();
          const form = event.currentTarget;
          const label = new FormData(form).get('label');
          void act(async () => {
            setToken((await api<{token: string}>('/auth/opds-tokens', json('POST', {label}))).token);
            form.reset();
            await list.refetch();
          });
        }}>
          <label>{t("Название устройства")}<input name="label" maxLength={80} required/></label>
          <button className="primary" disabled={busy}>{t("Выпустить токен")}</button>
        </form>
        {token && <label>{t("Сохраните токен. Он показывается один раз.")}<input readOnly value={token} onFocus={event => event.currentTarget.select()}/></label>}
        {list.error && <Notice error>{message(list.error)}</Notice>}
        <ul className="rows">{list.data?.map(entry => <li key={entry.id}>{entry.label}<button disabled={busy}
          onClick={() => act(async () => {await api('/auth/opds-tokens/' + entry.id, json('DELETE')); setToken(''); await list.refetch();})}>{t("Отозвать")}</button></li>)}</ul>
      </section>
      <section className="panel fields">
        <h2>KOReader Progress Sync</h2>
        <label>Custom Sync Server<input readOnly value={location.origin + '/koreader'}/></label>
        <p>{t("Имя пользователя ")}<strong>{user.username}</strong>{t(". Для KOReader выпустите отдельный пароль.")}</p>
        <div className="actions">
          <button disabled={busy} onClick={() => act(async () => {
            if (!confirm(t("Выпустить пароль KOReader? Предыдущий пароль перестанет действовать."))) return;
            setKoPassword((await api<{password: string}>('/devices/koreader/credentials', json('POST'))).password);
          })}>{t("Выпустить пароль")}</button>
          <button disabled={busy} onClick={() => act(async () => {
            await api('/devices/koreader/credentials', json('DELETE'));
            setKoPassword('');
            setNotice({text:t("Пароль KOReader отозван")});
          })}>{t("Отозвать")}</button>
        </div>
        {koPassword && <label>{t("Сохраните пароль. Он показывается один раз.")}<input value={koPassword} readOnly
          onFocus={event => event.currentTarget.select()}/></label>}
        <button onClick={() => setShowDocuments(!showDocuments)} aria-expanded={showDocuments}>{t("Документы KOReader")}</button>
        {showDocuments && <>
          <p>{t("Сохраните позицию из KOReader, затем скопируйте идентификатор и укажите его на странице нужной книги.")}</p>
          {devices.error && <Notice error>{message(devices.error)}</Notice>}
          {devices.data?.length === 0 && <p>{t("Документов пока нет.")}</p>}
          <ul className="rows">{devices.data?.map(item => <li key={item.document}>
            <div><code>{item.document}</code><small>{item.device || t("Ожидает синхронизации")}{item.percentage !== null && ' · ' + Math.round(item.percentage * 100) + '%'}</small></div>
            <small>{item.file_id ? t("Связан") : t("Ожидает привязки")}</small>
          </li>)}</ul>
        </>}
      </section>
      <AccountSettings user={user}/>
    </div>
  </>;
}


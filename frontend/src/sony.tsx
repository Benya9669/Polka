import {currentLanguage, t} from './locale';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {api, json, type BookFile, type Job, type Library, type Work} from './api';
import {message, Notice} from './main';
import {useToast} from './toasts';

type Selected = {id: number; title: string; format: string; work_id: number};
type Planned = {file_id: number; title: string; filename: string; source_format: string; format: string};
type Saved = {file_id: number; title: string; work_id: number; percentage: number | null; page: number | null; finished: boolean};
const endpoint = '/devices/sony';
const packageLimit = 25;
const jobLabels: Record<string, string> = {queued:'В очереди', running:'Подготовка', done:'Готово', error:'Ошибка'};

export function SonyPage() {
  const notify = useToast();
  const client = useQueryClient();
  const [params] = useSearchParams();
  const preselected = Number(params.get('file')) || 0;
  const initial = useQuery({queryKey:['file', preselected], enabled: preselected > 0,
    queryFn: () => api<Selected>('/files/' + preselected + '/info')});
  const [selected, setSelected] = useState<Selected[]>([]);
  const [removedInitial, setRemovedInitial] = useState(false);
  const [term, setTerm] = useState('');
  const [page, setPage] = useState(1);
  const [plan, setPlan] = useState<Planned[]>();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [progressId, setProgressId] = useState<number>();
  const picked = initial.data && !removedInitial && !selected.some(file => file.id === initial.data?.id)
    ? [initial.data, ...selected] : selected;
  const jobs = useQuery({queryKey:['sony-exports'], queryFn: () => api<Job[]>(endpoint + '/exports'),
    refetchInterval: result => result.state.data?.some(job => ['queued','running'].includes(job.status)) ? 1500 : false});
  const progress = useQuery({queryKey:['sony-progress'], queryFn: () => api<Saved[]>(endpoint + '/progress')});
  const libraries = useQuery({queryKey:['libraries'], queryFn: () => api<Library[]>('/libraries')});
  const catalog = useQuery({queryKey:['books', 'sony', term, page],
    queryFn: () => api<{items: Work[]; has_next: boolean}>('/books?' + new URLSearchParams({q:term, page:String(page), limit:'12'}))});
  const currentFile = progressId || picked[0]?.id;
  const report = progress.data?.find(item => item.file_id === currentFile);

  function change(work: Work, file: BookFile, enabled: boolean) {
    setPlan(undefined);
    if (enabled) {
      setSelected(previous => [...previous, {id:file.id, title:work.title, format:file.format, work_id:work.id}]);
    } else {
      remove(file.id);
    }
  }
  async function selectAll() {
    const files = [...picked];
    const included = new Set(files.map(file => file.work_id));
    let nextPage = 1;
    let more = true;
    while (more) {
      const query = new URLSearchParams({q:term, page:String(nextPage++), limit:'100'});
      const result = await api<{items: Work[]; has_next: boolean}>('/books?' + query);
      for (const work of result.items) {
        if (included.has(work.id)) continue;
        const editions = work.editions.flatMap(edition => edition.files);
        const file = editions.find(item => item.format === 'epub') || editions.find(item => item.format === 'pdf') || editions[0];
        if (file) {
          files.push({id:file.id, title:work.title, format:file.format, work_id:work.id});
          included.add(work.id);
        }
      }
      more = result.has_next;
    }
    setSelected(files);
    setPlan(undefined);
  }
  function remove(id: number) {
    setPlan(undefined);
    if (id === preselected) setRemovedInitial(true);
    setSelected(previous => previous.filter(file => file.id !== id));
    if (progressId === id) setProgressId(undefined);
  }
  async function act(action: () => Promise<void>) {
    setNotice(undefined);
    setBusy(true);
    try {await action();} catch (e) {setNotice({text:message(e), error:true});} finally {setBusy(false);}
  }
  const errors = initial.error || catalog.error || jobs.error || progress.error;
  return <>
    <header className="heading"><div><h1>Sony PRS-600</h1><p>{t("Подготовьте книги для переноса по USB или на карте памяти.")}</p></div></header>
    {(errors || notice) && <Notice error={Boolean(errors || notice?.error)}>{notice?.text || message(errors)}</Notice>}
    <div className="split">
      <section className="panel">
        <h2>{t("Выбрать книги")}</h2>
        <form className="filters" onSubmit={event => {
          event.preventDefault();
          setTerm(String(new FormData(event.currentTarget).get('q') || '').trim());
          setPage(1);
        }}>
          <input name="q" type="search" aria-label={t("Поиск книг для Sony")} placeholder={t("Название или автор")}/>
          <button disabled={busy}>{t("Найти")}</button>
        </form>
        <button disabled={busy || !catalog.data?.items.length} onClick={() => act(selectAll)}>
          {term ? t("Выбрать все найденные книги") : t("Выбрать все книги")}
        </button>
        {!!picked.length && <button disabled={busy} onClick={() => {
          setSelected([]); setRemovedInitial(true); setProgressId(undefined); setPlan(undefined);
        }}>{t("Снять выбор")}</button>}
        {catalog.isPending && <p aria-busy="true">{t("Загружаем книги…")}</p>}
        {catalog.data?.items.length === 0 && <p>{t("Книги не найдены.")}</p>}
        <ul className="rows">{catalog.data?.items.map(work => <li key={work.id}><div>
          <strong>{work.title}</strong><small>{[work.authors.join(', '),
            libraries.data?.find(library => library.id === work.library_id)?.name].filter(Boolean).join(' · ')}</small>
          {work.editions.flatMap(edition => edition.files).map(file => <label className="check" key={file.id}>
            <input type="checkbox" disabled={busy} checked={picked.some(item => item.id === file.id)}
              onChange={event => change(work, file, event.target.checked)}/>
            <span>{file.format.toUpperCase()} · {file.name}</span>
          </label>)}
        </div></li>)}</ul>
        {(page > 1 || catalog.data?.has_next) && <div className="pager">
          <button disabled={page === 1} onClick={() => setPage(page - 1)}>{t("Назад")}</button>
          <span>{t("Страница ")}{page}</span>
          <button disabled={!catalog.data?.has_next} onClick={() => setPage(page + 1)}>{t("Далее")}</button>
        </div>}
      </section>
      <div>
        <section className="panel">
          <h2>{t("Пакет для Sony")}</h2>
          {picked.length ? <ul className="rows">{picked.map(item => <li key={item.id}>
            <div><strong>{item.title}</strong><small>{item.format.toUpperCase()}</small></div>
            <button disabled={busy} onClick={() => remove(item.id)} aria-label={t("Убрать ") + item.title}>{t("Убрать")}</button>
          </li>)}</ul> : <p>{t("Выберите хотя бы один файл из каталога.")}</p>}
          {!!picked.length && <button disabled={busy} onClick={() => act(async () => {
            const files: Planned[] = [];
            for (let i = 0; i < picked.length; i += packageLimit) {
              const ids = picked.slice(i, i + packageLimit).map(item => item.id);
              const data = await api<{files: Planned[]}>(endpoint + '/preview', json('POST', {file_ids:ids}));
              files.push(...data.files);
            }
            setPlan(files);
          })}>{t("Посмотреть план экспорта")}</button>}
          {plan && <>
            <p>{t("Файлов: {files}. ZIP-пакетов: {packages}.", {files:plan.length, packages:Math.ceil(plan.length / packageLimit)})}</p>
            <ul className="rows">{plan.map(item => <li key={item.file_id}><div><strong>{item.title}</strong>
              <small>{item.source_format.toUpperCase()} → {item.format === 'epub' ? 'EPUB 2' : 'PDF'}</small></div></li>)}</ul>
            <button className="primary" disabled={busy} onClick={() => act(async () => {
              for (let i = 0; i < plan.length; i += packageLimit) {
                const ids = plan.slice(i, i + packageLimit).map(item => item.file_id);
                await api(endpoint + '/exports', json('POST', {file_ids:ids}));
                const remaining = plan.slice(i + packageLimit);
                setPlan(remaining.length ? remaining : undefined);
                void jobs.refetch();
              }
              notify("Подготовка пакета ZIP поставлена в очередь");
              await jobs.refetch();
            })}>{t("Подготовить ZIP")}</button>
          </>}
        </section>
        <section className="panel">
          <h2>{t("Пакеты для переноса")}</h2>
          {jobs.data?.length === 0 && <p>{t("Здесь появятся подготовленные пакеты.")}</p>}
          <ul className="rows">{jobs.data?.map(job => <li key={job.id}><div>
            <strong>{t(jobLabels[job.status] || job.status)}</strong>
            {job.result?.titles?.length ? <small>{job.result.titles.join(', ')}</small> : null}
            <small>{new Date(job.created).toLocaleString(currentLanguage())}</small>
            {job.error && <small className="error-text">{t(job.error)}</small>}
          </div>{job.status === 'done' && <a className="button" href={'/api/v1' + endpoint + '/exports/' + job.id + '/archive'}>{t("Скачать ZIP")}</a>}</li>)}</ul>
        </section>
        <section className="panel">
          <h2>{t("Прогресс на Sony")}</h2>
          {picked.length ? <form className="fields" key={currentFile + ':' + JSON.stringify(report)} onSubmit={event => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const percentage = String(data.get('percentage') || '');
            const pageValue = String(data.get('page') || '');
            void act(async () => {
              await api(endpoint + '/progress/' + currentFile, json('PUT', {
                percentage: percentage ? Number(percentage) / 100 : null, page: pageValue ? Number(pageValue) : null,
                finished:data.has('finished'),
              }));
              notify("Позиция Sony сохранена");
              await Promise.all([progress.refetch(), client.invalidateQueries({queryKey:['reading-home']}),
                client.invalidateQueries({queryKey:['reading-state']}), client.invalidateQueries({queryKey:['progress']})]);
            });
          }}>
            <label>{t("Книга")}<select value={currentFile} onChange={event => setProgressId(Number(event.target.value))}>
              {picked.map(item => <option value={item.id} key={item.id}>{item.title} · {item.format.toUpperCase()}</option>)}
            </select></label>
            <label>{t("Прочитано, %")}<input name="percentage" type="number" min={0} max={100} step="any"
              defaultValue={report?.percentage != null ? report.percentage * 100 : ''}/></label>
            <label>{t("Страница на устройстве")}<input name="page" type="number" min={1} max={100000} defaultValue={report?.page ?? ''}/></label>
            <label className="check"><input type="checkbox" name="finished" defaultChecked={report?.finished}/>{t(" Книга прочитана")}</label>
            <button disabled={busy}>{t("Сохранить позицию")}</button>
          </form> : <p>{t("Выберите в каталоге файл, который вы читаете на Sony.")}</p>}
          <ul className="rows">{progress.data?.map(item => <li key={item.file_id}>
            <div><Link to={'/books/' + item.work_id}>{item.title}</Link>
              <small>{item.finished ? t("Прочитано") : [item.percentage != null && Math.round(item.percentage * 100) + '%',
                item.page && t("Страница ") + item.page].filter(Boolean).join(' · ')}</small></div>
          </li>)}</ul>
        </section>
      </div>
    </div>
  </>;
}


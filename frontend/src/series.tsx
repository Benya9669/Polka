import {t} from './locale';
import {useQuery} from '@tanstack/react-query';
import {useState} from 'react';
import {Link, useParams, useSearchParams} from 'react-router-dom';
import {api, type SeriesMembership, type Work} from './api';
import {Pagination} from './authors';
import {useLibraries} from './library';
import {message, Notice} from './main';

type Branch = {id: number; parent_id: number | null; library_id: number; name: string; path: string[]};
type Detail = Branch & {children: Branch[]; children_next: boolean; books: Work[]; has_next: boolean};

export function SeriesPage() {
  const [params, setParams] = useSearchParams();
  const term = params.get('q') || '';
  const library = params.get('library_id') || '';
  const page = Math.max(1, Number(params.get('page')) || 1);
  const libs = useLibraries();
  const result = useQuery({queryKey:['series',params.toString()], queryFn:() =>
    api<{items: Branch[]; has_next: boolean}>('/series?' + params)});
  return <>
    <header className="heading"><h1>{t("Серии")}</h1></header>
    <form className="filters" onSubmit={event => {
      event.preventDefault();
      const term = String(new FormData(event.currentTarget).get('q') || '').trim();
      const next = new URLSearchParams(params);
      next.delete('page');
      if (term) next.set('q',term); else next.delete('q');
      setParams(next);
    }}>
      <input type="search" name="q" key={term} defaultValue={term} maxLength={500} aria-label={t("Название серии")} placeholder={t("Название серии или подсерии")}/>
      <select aria-label={t("Библиотека")} value={library} onChange={event => {
        const next = new URLSearchParams(params);
        next.delete('page');
        if (event.target.value) next.set('library_id',event.target.value); else next.delete('library_id');
        setParams(next);
      }}><option value="">{t("Все библиотеки")}</option>{libs.data?.map(lib => <option key={lib.id} value={lib.id}>{lib.name}</option>)}</select>
      <button>{t("Найти")}</button>
    </form>
    {result.isPending && <p role="status">{t("Загружаем серии…")}</p>}
    {result.error && <Notice error>{message(result.error)}</Notice>}
    {result.data?.items.length === 0 && <p>{t("Серии не найдены.")}</p>}
    {!!result.data?.items.length && <ul className="rows">{result.data.items.map(branch => <li key={branch.id}><div>
      <Link to={'/series/' + branch.id}>{branch.path.join(' / ')}</Link>
      <small>{libs.data?.find(lib => lib.id === branch.library_id)?.name}</small>
    </div></li>)}</ul>}
    <Pagination page={page} more={result.data?.has_next} change={next => {
      const query = new URLSearchParams(params); query.set('page', String(next)); setParams(query);
    }}/>
  </>;
}

export function SeriesDetail() {
  const {id} = useParams();
  const [params,setParams] = useSearchParams();
  const page = Math.max(1, Number(params.get('page')) || 1);
  const childrenPage = Math.max(1, Number(params.get('children_page')) || 1);
  const libs = useLibraries();
  const result = useQuery({queryKey:['series',id,page,childrenPage], queryFn:() =>
    api<Detail>('/series/' + id + '?page=' + page + '&children_page=' + childrenPage)});
  if (!result.data) return result.error ? <Notice error>{message(result.error)}</Notice> : <p role="status">{t("Открываем серию…")}</p>;
  const branch = result.data;
  function change(field: string, next: number) {
    const query = new URLSearchParams(params); query.set(field,String(next)); setParams(query);
  }
  return <>
    <Link className="back" to={branch.parent_id ? '/series/' + branch.parent_id : '/series'}>
      {branch.parent_id ? branch.path.at(-2) : t("Все серии")}</Link>
    <header className="heading"><div><h1>{branch.name}</h1><p>{[
      libs.data?.find(lib => lib.id === branch.library_id)?.name, ...branch.path.slice(0,-1),
    ].filter(Boolean).join(' / ')}</p></div></header>
    {(!!branch.children.length || childrenPage > 1) && <section aria-labelledby="subseries-heading">
      <h2 id="subseries-heading">{t("Подсерии")}</h2>
      <ul className="rows">{branch.children.map(child => <li key={child.id}><Link to={'/series/' + child.id}>{child.name}</Link></li>)}</ul>
      <Pagination page={childrenPage} more={branch.children_next} change={next => change('children_page',next)}/>
    </section>}
    {(!!branch.books.length || page > 1) && <section aria-labelledby="volumes-heading">
      <h2 id="volumes-heading">{t("Тома")}</h2>
      <ul className="rows">{branch.books.map(book => {
        const position = book.series_memberships.find(entry => entry.id === branch.id)?.position;
        return <li key={book.id}>
          <div><strong><Link to={'/books/' + book.id}>{book.title}</Link></strong><small>{book.authors.join(', ')}</small></div>
          {position && <span>{t("Том ")}{position}</span>}
        </li>;
      })}</ul>
      <Pagination page={page} more={branch.has_next} change={next => change('page', next)}/>
    </section>}
  </>;
}

export function SeriesEditor({initial, initialPosition}: {initial: SeriesMembership[]; initialPosition: string}) {
  const [entries,setEntries] = useState(initial.map(({path,position}) => ({path,position})));
  function replace(index: number, entry: {path: string[]; position: string}) {
    setEntries(values => values.map((value, at) => at === index ? entry : value));
  }
  return <fieldset className="fields series-editor">
    <legend>{t("Серии и подсерии")}</legend>
    <input type="hidden" name="series_memberships" value={JSON.stringify(entries)}/>
    {!entries.length && <label>{t("Номер без серии")}<input name="series_index" maxLength={40} defaultValue={initialPosition}/></label>}
    {entries.map((entry,index) => <div className="fields series-editor-entry" key={index}>
      {entry.path.map((name,level) => <label key={level}>
        {level === 0 ? t("Название серии") : t("Название подсерии")}
        <input required maxLength={500} value={name} onChange={event => replace(index, {...entry,
          path:entry.path.map((item, at) => at === level ? event.target.value : item)})}/>
      </label>)}
      <label>{t("Номер тома")}<input maxLength={40} value={entry.position} onChange={event => replace(index, {...entry,position:event.target.value})}/></label>
      <div className="actions">
        {entry.path.length < 16 && <button type="button" onClick={() => replace(index,{...entry,path:[...entry.path,'']})}>{t("Добавить подсерию")}</button>}
        {entry.path.length > 1 && <button type="button" onClick={() => replace(index,{...entry,path:entry.path.slice(0,-1)})}>{t("Убрать последнюю подсерию")}</button>}
        <button type="button" onClick={() => setEntries(values => values.filter((_,at) => at !== index))}>{t("Убрать из серии")}</button>
      </div>
    </div>)}
    {entries.length < 64 && <button type="button" onClick={() => setEntries(values => [...values,{path:[''],position:''}])}>{t("Добавить серию")}</button>}
  </fieldset>;
}

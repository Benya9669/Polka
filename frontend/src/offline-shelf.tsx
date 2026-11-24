import {t} from './locale';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';
import {Link} from 'react-router-dom';
import type {User} from './api';
import {message, Notice} from './main';
import {offlineSupported, removeBook, savedFiles} from './offline';

export function OfflineShelf({user}: {user: User}) {
  const client = useQueryClient();
  const [error,setError] = useState('');
  const [busy,setBusy] = useState<number>();
  const supported = offlineSupported();
  const result = useQuery({queryKey:['offline-files',user.id], queryFn:() => savedFiles(user.id), networkMode:'always', enabled:supported});
  return <>
    <header className="heading"><div><h1>{t("Книги без сети")}</h1><p>{user.username}</p></div></header>
    {!supported && <Notice>{t("Откройте Полку через HTTPS или localhost, чтобы сохранять книги в браузере.")}{' '}
      <a href="https://github.com/Benya9669/Polka/blob/main/docs/configuration.md">{t("Настройка HTTPS")}</a></Notice>}
    {supported && result.isPending && <p role="status">{t("Открываем сохранённые книги…")}</p>}
    {(error || result.error) && <Notice error>{error || message(result.error)}</Notice>}
    {result.data?.length === 0 && <section className="empty"><h2>{t("Сохранённых книг пока нет")}</h2><p>{t("Откройте EPUB или PDF и нажмите «Сохранить без сети».")}</p></section>}
    {!!result.data?.length && <ul className="rows">{result.data.map(({file}) => <li key={file.id}>
      <div><strong><Link to={'/read/' + file.id}>{file.title}</Link></strong><small>{file.format.toUpperCase()} · {file.name}</small></div>
      <button disabled={busy !== undefined} onClick={async () => {
        setBusy(file.id); setError('');
        try {
          await removeBook(user.id,file.id);
          await Promise.all([result.refetch(),client.invalidateQueries({queryKey:['offline-copy',user.id,file.id]})]);
        } catch (e) {setError(message(e));} finally {setBusy(undefined);}
      }}>{t("Убрать копию")}</button>
    </li>)}</ul>}
  </>;
}

import {currentLanguage, t} from './locale';
import {useQuery} from '@tanstack/react-query';
import {useState, type FormEvent} from 'react';
import {api, json, type User} from './api';
import {message, Notice} from './main';
import {useToast} from './toasts';

type Role = 'admin' | 'member' | 'reader';
type Session = {id: string; current: boolean; expires: string};
const roles: Record<Role, string> = {admin:'Администратор', member:'Пользователь', reader:'Только чтение'};
const role = (user: User): Role => user.admin ? 'admin' : user.read_only ? 'reader' : 'member';

function RolePicker({value, onChange, disabled}: {value?: Role; onChange?: (role: Role) => void; disabled?: boolean}) {
  return <select name="role" aria-label={t("Роль")} value={value} defaultValue={value === undefined ? 'member' : undefined}
    disabled={disabled} onChange={event => onChange?.(event.target.value as Role)}>
    {Object.entries(roles).map(([key, label]) => <option value={key} key={key}>{t(label)}</option>)}
  </select>;
}

export function AccountSettings({user}: {user: User}) {
  const notify = useToast();
  const sessions = useQuery({queryKey: ['sessions'], queryFn: () => api<Session[]>('/auth/sessions')});
  const users = useQuery({queryKey: ['account-users'], queryFn: () => api<User[]>('/auth/users'), enabled: user.admin});
  const [notice, setNotice] = useState<{text: string; error?: boolean}>();
  const [busy, setBusy] = useState(false);
  const [reset, setReset] = useState<number>();
  async function run(action: () => Promise<unknown>, text: string) {
    setNotice(undefined);
    setBusy(true);
    try {await action(); notify(text);}
    catch (e) {setNotice({text: message(e), error: true});}
    finally {setBusy(false);}
  }
  function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form));
    void run(async () => {
      await api('/auth/password', json('PUT', payload));
      form.reset();
      await sessions.refetch();
    }, t("Пароль изменён. Остальные сеансы завершены."));
  }
  function createUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const {username, password, role: selected} = Object.fromEntries(new FormData(form));
    void run(async () => {
      await api('/auth/users', json('POST', {username, password, admin:selected === 'admin', read_only:selected === 'reader'}));
      form.reset();
      await users.refetch();
    }, t("Пользователь создан"));
  }
  return <>
    {notice && <Notice error={notice.error}>{notice.text}</Notice>}
    <section className="panel fields">
      <h2>{t("Пароль и сеансы")}</h2>
      <form className="fields" onSubmit={changePassword}>
        <label>{t("Текущий пароль")}<input name="current_password" type="password" required maxLength={256} autoComplete="current-password"/></label>
        <label>{t("Новый пароль")}<input name="password" type="password" required minLength={10} maxLength={256} autoComplete="new-password"/></label>
        <button disabled={busy}>{t("Сменить пароль")}</button>
      </form>
      {sessions.error && <Notice error>{message(sessions.error)}</Notice>}
      <ul className="rows">{sessions.data?.map(session => <li key={session.id}>
        <div>{session.current ? t("Текущий браузер") : t("Другой браузер")}<small>{t("Действует до ")}{new Date(session.expires).toLocaleDateString(currentLanguage())}</small></div>
        {!session.current && <button disabled={busy} onClick={() => run(async () => {
          await api('/auth/sessions/' + session.id, json('DELETE'));
          await sessions.refetch();
        }, t("Сеанс завершён"))}>{t("Завершить")}</button>}
      </li>)}</ul>
    </section>
    {user.admin && <section className="panel fields">
      <h2>{t("Пользователи")}</h2>
      <form className="fields" onSubmit={createUser}>
        <label>{t("Имя")}<input name="username" required minLength={2} maxLength={64}/></label>
        <label>{t("Пароль")}<input type="password" name="password" autoComplete="new-password" minLength={10} maxLength={256} required/></label>
        <label>{t("Роль ")}<RolePicker/></label>
        <button className="primary" disabled={busy}>{t("Создать пользователя")}</button>
      </form>
      {users.error && <Notice error>{message(users.error)}</Notice>}
      <ul className="rows account-users">{users.data?.map(entry => <li key={entry.id}>
        <div><strong>{entry.username}</strong><small>{t(roles[role(entry)])}</small></div>
        {entry.id !== user.id && <div className="fields">
          <RolePicker value={role(entry)} disabled={busy} onChange={selected => run(async () => {
            await api('/auth/users/' + entry.id + '/role', json('PATCH', {role:selected}));
            await users.refetch();
          }, t("Роль обновлена"))}/>
          {reset === entry.id ? <form className="fields" onSubmit={event => {
            event.preventDefault();
            const password = new FormData(event.currentTarget).get('password');
            void run(async () => {
              await api('/auth/users/' + entry.id + '/password', json('PUT', {password}));
              setReset(undefined);
            }, t("Пароль сброшен. Сеансы и токены пользователя отозваны."));
          }}>
            <label>{t("Новый пароль")}<input name="password" type="password" required minLength={10} maxLength={256} autoComplete="new-password"/></label>
            <div className="actions"><button disabled={busy}>{t("Сохранить")}</button><button type="button" onClick={() => setReset(undefined)}>{t("Отмена")}</button></div>
          </form> : <button onClick={() => setReset(entry.id)} disabled={busy}>{t("Сбросить пароль")}</button>}
        </div>}
      </li>)}</ul>
    </section>}
  </>;
}


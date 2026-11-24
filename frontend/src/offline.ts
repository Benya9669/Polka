import {t} from './locale';
import {api, forUser, type BookFile, type Position, type User} from './api';

export type ReaderInfo = BookFile & {title: string; work_id: number; sha256: string};
export type SavedMark = {id: number; label: string; locator: string};
export type SavedAnnotation = {id: number; locator: string; quote: string; note: string};
type SavedReading = {position: Position; marks: SavedMark[]; annotations: SavedAnnotation[]};
export type SavedBook = SavedReading & {userId: number; fileId: number; file: ReaderInfo; content: Blob};
export const identityKey = 'polka:offline-user';

export function lastUser(): User | undefined {
  try { return JSON.parse(localStorage.getItem(identityKey) || 'null') || undefined; }
  catch { return undefined; }
}

export function rememberUser(user?: User) {
  try {
    if (user) localStorage.setItem(identityKey, JSON.stringify(user));
    else localStorage.removeItem(identityKey);
  } catch { /* Offline access requires browser storage. */ }
}

export function offlineSupported() {
  return window.isSecureContext && 'serviceWorker' in navigator && 'indexedDB' in window;
}

function requestResult<T>(request: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function books<T>(mode: IDBTransactionMode, operation: (store: IDBObjectStore) => Promise<T>): Promise<T> {
  const opening = indexedDB.open('polka-offline', 1);
  opening.onupgradeneeded = () => opening.result.createObjectStore('books', {keyPath:['userId','fileId']}).createIndex('owner','userId');
  const db = await requestResult(opening);
  try {
    const transaction = db.transaction('books', mode);
    const complete = new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error);
    });
    const [result] = await Promise.all([operation(transaction.objectStore('books')), complete]);
    return result;
  } finally { db.close(); }
}

export function savedBook(userId: number, fileId: number) {
  return books<SavedBook | undefined>('readonly', store => requestResult(store.get([userId,fileId])));
}

export function savedFiles(userId: number) {
  return books<Pick<SavedBook, 'file'>[]>('readonly', async store => {
    const result = [];
    const request = store.index('owner').openCursor(IDBKeyRange.only(userId));
    let row = await requestResult(request);
    while (row) {
      result.push({file:(row.value as SavedBook).file});
      row.continue();
      row = await requestResult(request);
    }
    return result;
  });
}

export function removeBook(userId: number, fileId: number) {
  return books('readwrite', store => requestResult(store.delete([userId,fileId])));
}

export function clearCopies(userId: number) {
  return books('readwrite', async store => {
    const keys = await requestResult(store.index('owner').getAllKeys(userId));
    await Promise.all(keys.map(key => requestResult(store.delete(key))));
  });
}

export function refreshSavedReading(userId: number, fileId: number, changes: Partial<SavedReading>) {
  if (!offlineSupported()) return Promise.resolve();
  return books('readwrite', async store => {
    const current = await requestResult(store.get([userId,fileId])) as SavedBook | undefined;
    if (current) await requestResult(store.put({...current,...changes}));
  });
}

async function readyWorker() {
  await navigator.serviceWorker.register('/offline-worker.js');
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([navigator.serviceWorker.ready, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(t("Не удалось подготовить офлайн-читалку"))), 15000);
    })]);
  } finally {clearTimeout(timer);}
}

export async function saveBook(userId: number, file: ReaderInfo) {
  if (!offlineSupported()) throw new Error(t("Для чтения без сети откройте Полку через HTTPS или localhost"));
  const fileId = file.id;
  const [response, position, marks, annotations] = await Promise.all([
    fetch('/api/v1/files/' + fileId + '?inline=true', forUser(userId,{cache:'no-store'})),
    api<Position>('/reading/progress/' + fileId,forUser(userId)), api<SavedMark[]>('/reading/bookmarks/' + fileId,forUser(userId)),
    api<SavedAnnotation[]>('/reading/annotations/' + fileId,forUser(userId)), readyWorker(),
  ]);
  if (!response.ok) throw new Error(t("Не удалось сохранить файл для чтения без сети"));
  const content = await response.blob();
  if (content.size !== file.size) throw new Error(t("Скачанный файл не совпадает с карточкой. Откройте книгу снова."));
  if (lastUser()?.id !== userId) throw new Error(t("Сеанс изменился. Откройте книгу снова."));
  await books('readwrite', store => requestResult(store.put({userId,fileId,file,content,position,marks,annotations} satisfies SavedBook)));
}

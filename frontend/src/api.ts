export class ApiError extends Error {
  constructor(public status: number, public body: {detail?: unknown; current?: Position}) {
    super(typeof body.detail === 'string' ? body.detail : status === 422 ? 'Проверьте введённые данные' : 'Не удалось выполнить запрос');
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (typeof init?.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const res = await fetch('/api/v1' + path, {
    ...init,
    cache: 'no-store',
    headers,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body);
  }
  return res.status === 204 ? undefined as T : res.json();
}

export function forUser(userId: number, init?: RequestInit): RequestInit {
  const headers = new Headers(init?.headers);
  headers.set('X-Polka-User', String(userId));
  return {...init,headers};
}

export function json(method: string, value?: unknown): RequestInit {
  return {method, body: value === undefined ? undefined : JSON.stringify(value)};
}

export type User = {id: number; username: string; admin: boolean; read_only: boolean};
export type Library = {id: number; name: string; description: string; mode: 'managed' | 'indexed'; folder: string; owner_id: number; shared: boolean;
  formats: string[]; path_template: string; active_layout_id: number | null};
export type ScanSettings = {mode: 'off' | 'preview' | 'apply'; interval_minutes: number; folder: string;
  formats: string[]; watch: boolean; next_scan: string | null};
export type BookFile = {id: number; name: string; format: string; size: number; derived_from: number | null};
export type Edition = {id: number; cover: string | null; files: BookFile[]; summary: string; language: string;
  publisher: string; year: string; isbn: string; metadata_source: string};
export type SeriesMembership = {id: number; path: string[]; position: string};
export type Work = {id: number; title: string; authors: string[]; editions: Edition[]; library_id: number;
  author_credits: {id: number | null; name: string; credited_as: string}[];
  series_memberships: SeriesMembership[];
  series: string; series_index: string; genres: string[]; tags: string[]; custom_fields: Record<string, string>; trashed_at: string | null};
export type Job = {id: number; kind: string; status: string; error: string | null; created: string; library_id: number | null; result: {
  found?: number; duplicates?: number; candidates?: number; conversions?: number; confirmed?: number; reused?: number; automatic?: boolean;
  new_works?: number; unreadable?: number;
  items?: {path: string; sha256: string; title: string; format: string; duplicate: boolean; existing_duplicate?: boolean;
    match_reason: string | null; convert: boolean;
    language?: string; isbn?: string; incoming?: {path: string; title: string; format: string; confidence: number; reason: string}[];
    confidence?: number; matches?: {work_id: number; confidence: number; reason: string; title: string}[]}[];
  added?: number; attached?: number; skipped?: number; errors?: {path: string; error: string}[]; title?: string; bytes?: number; total?: number;
  speed?: number; file_id?: number; duplicate?: boolean;
  stage?: 'downloading' | 'processing';
  undo_id?: number; work_ids?: number[]; trashed?: number; files?: number; location?: string; titles?: string[];
  source_id?: number; target_id?: number; source_title?: string; target_title?: string;
  source?: string; sha256?: string; format?: string; read_file_id?: number; conversion_id?: number;
  delivery?: 'library' | 'file'; expires_at?: string;
  provenance?: {identifier?: string; downloaded_at: string; metadata?: {authors?: string[]; title?: string}};
} | null};
export type Position = {version: number; locator?: string; locator_type?: 'epub_cfi' | 'pdf_page'; percentage?: number;
  updated?: string; device?: string; chapter?: string | null;
  koreader?: {locator: string; percentage: number; device: string};
  sony?: {percentage: number | null; page: number | null; finished: boolean; exact_page: boolean};
  alternate?: {file_id: number; format: string; percentage: number; updated: string}};
export type Point = {locator: string; percentage: number; chapter?: string | null};

export const bookExtensions = '.epub,.pdf,.fb2,.zip,.txt,.mobi,.azw3,.djvu,.cbz,.html,.htm,.xhtml,.rtf';
export const conversionTargets: Record<string, string> = {
  fb2:'EPUB', 'fb2.zip':'EPUB', txt:'EPUB', mobi:'EPUB', azw3:'EPUB', cbz:'EPUB', html:'EPUB', rtf:'EPUB', djvu:'PDF',
};


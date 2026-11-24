import {t} from './locale';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import ePub, {type Rendition} from 'epubjs';
import type {Location} from 'epubjs/types/rendition';
import type Section from 'epubjs/types/section';
import {Bookmark as BookmarkIcon, ChevronLeft, ChevronRight, Maximize, Minus, Plus} from 'lucide-react';
import {getDocument, GlobalWorkerOptions, type PDFDocumentProxy} from 'pdfjs-dist';
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import {useCallback, useEffect, useRef, useState} from 'react';
import {Link, useLocation, useParams} from 'react-router-dom';
import {api, ApiError, forUser, json, type Point, type Position, type User} from './api';
import {message, Notice} from './main';
import {chapterAt, chapterStarts, flattenChapters, type Chapter} from './reader-chapters';
import {offlineSupported, refreshSavedReading, saveBook, savedBook,
  type ReaderInfo as Info, type SavedAnnotation as Annotation, type SavedBook, type SavedMark as Mark} from './offline';

GlobalWorkerOptions.workerSrc = pdfWorker;
type Theme = 'light' | 'sepia' | 'dark';
type Margin = 'narrow' | 'normal' | 'wide';
const readingFonts = {serif:'Georgia, serif', sans:'system-ui, sans-serif', mono:'monospace'};
type FontFamily = keyof typeof readingFonts;
type Appearance = {font: number; theme: Theme; family: FontFamily; spacing: string};
type Pending = Point & {version: number};
type SearchHit = {cfi: string; excerpt: string};
type Selection = {locator: string; quote: string};

function cached(key: string): Pending | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null');
    if (value && typeof value.locator === 'string' && Number.isInteger(value.version)) return value;
  } catch {
    return undefined;
  }
}

function deviceName() {
  return /Android|iPhone|iPad/i.test(navigator.userAgent) ? 'Мобильный браузер' : 'Браузер ПК';
}

function useProgress(id: number, initial: Position, user: User) {
  const client = useQueryClient();
  const storage = 'polka:progress:' + user.id + ':' + id;
  const pending = useRef(cached(storage));
  const version = useRef(pending.current?.version ?? initial.version);
  const point = useRef<Point | undefined>(pending.current || (initial.locator ?
    {locator: initial.locator, percentage: initial.percentage || 0, chapter: initial.chapter} : undefined));
  const [shown, setShown] = useState(point.current);
  const [status, setStatus] = useState('');
  const [conflict, setConflict] = useState<Position>();
  const conflictRef = useRef<Position | undefined>(undefined);
  const busy = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const active = useRef(true);

  const persist = useCallback((previous?: Pending) => {
    try {
      if (previous && localStorage.getItem(storage) !== JSON.stringify(previous)) return;
      if (pending.current) localStorage.setItem(storage, JSON.stringify(pending.current));
      else localStorage.removeItem(storage);
    } catch {
      // A browser can disable local storage.
    }
  }, [storage]);

  const rememberServerPosition = useCallback((saved: Position) => {
    client.setQueryData(['progress', id], saved);
    client.setQueryData<ReaderLaunch>(['reader-launch', user.id, id], previous => previous && {...previous,position:saved});
    void refreshSavedReading(user.id,id,{position:saved}).catch(() => {});
  }, [client, id, user.id]);

  const flush = useCallback(async function send(keepalive = false) {
    if (!pending.current || busy.current || conflictRef.current) return;
    const next = pending.current;
    busy.current = true;
    let replay = false;
    try {
      const saved = await api<Position>('/reading/progress/' + id, forUser(user.id,{
        ...json('PUT', {...next, device: deviceName()}), keepalive,
      }));
      version.current = saved.version;
      const previous = pending.current;
      pending.current = previous && previous !== next ? {...previous, version: saved.version} : undefined;
      if (previous) persist(previous);
      replay = Boolean(pending.current);
      if (active.current) setStatus(t("Позиция сохранена"));
      void client.invalidateQueries({queryKey: ['history']});
      void client.invalidateQueries({queryKey: ['reading-home']});
      void client.invalidateQueries({queryKey: ['reading-shelf']});
      void client.invalidateQueries({queryKey: ['reading-state']});
      rememberServerPosition(saved);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.body.current) {
        conflictRef.current = error.body.current;
        if (active.current) setConflict(error.body.current);
      } else if (active.current) {
        setStatus(t("Позиция сохранена в браузере. Повторим отправку."));
      }
    } finally {
      busy.current = false;
      if (replay) void send(keepalive);
    }
  }, [id, user.id, client, persist, rememberServerPosition]);

  const update = useCallback((value: Point) => {
    if (point.current?.locator === value.locator && (point.current.chapter ?? null) === (value.chapter ?? null)) return;
    point.current = value;
    setShown(value);
    pending.current = {...value, version: version.current};
    persist();
    setStatus(t("Сохраняем позицию…"));
    clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), 650);
  }, [flush, persist]);

  useEffect(() => {
    active.current = true;
    const sync = () => void flush();
    const hide = () => {if (document.visibilityState === 'hidden') void flush(true);};
    const leave = () => void flush(true);
    window.addEventListener('online', sync);
    window.addEventListener('pagehide', leave);
    document.addEventListener('visibilitychange', hide);
    const retry = setInterval(sync, 6000);
    sync();
    return () => {
      active.current = false;
      window.removeEventListener('online', sync);
      window.removeEventListener('pagehide', leave);
      document.removeEventListener('visibilitychange', hide);
      clearTimeout(timer.current);
      clearInterval(retry);
      leave();
    };
  }, [flush]);

  function resolve(useLocal: boolean, go: (locator: string) => void) {
    const current = conflictRef.current;
    if (!current) return;
    version.current = current.version;
    conflictRef.current = undefined;
    setConflict(undefined);
    const previous = pending.current;
    if (useLocal && previous) {
      pending.current = {...previous, version: version.current};
      persist(previous);
      void flush();
    } else {
      pending.current = undefined;
      if (previous) persist(previous);
      rememberServerPosition(current);
      if (current.locator) {
        point.current = {locator: current.locator, percentage: current.percentage || 0, chapter: current.chapter};
        setShown(point.current);
        go(current.locator);
      }
      setStatus(t("Открыта позиция с другого устройства"));
    }
  }

  return {point: shown, status, conflict, update, resolve, start: useRef(point.current?.locator).current};
}

function applyAppearance(view: Rendition, appearance: Appearance) {
  view.themes.fontSize(appearance.font + '%');
  view.themes.override('font-family', readingFonts[appearance.family], true);
  view.themes.override('line-height', appearance.spacing, true);
  view.themes.override('color', appearance.theme === 'dark' ? '#e7e1d2' : '#242621', true);
}

function EpubView({fileId, userId, content, start, update, register, font, theme, family, spacing, margin, flow,
  onError, onChapters, onSelection, highlights}: Appearance & {
  fileId: number; userId: number; content?: Blob; start?: string; update: (point: Point) => void;
  register: (controller: Controller | null) => void; margin: Margin; flow: boolean;
  onError: (error: string) => void; onChapters: (chapters: Chapter[]) => void;
  onSelection: (selection: Selection) => void; highlights: Annotation[];
}) {
  const element = useRef<HTMLDivElement>(null);
  const rendition = useRef<Rendition | undefined>(undefined);
  const appearance = useRef({font, theme, family, spacing});
  appearance.current = {font, theme, family, spacing};
  const applied = useRef(new Set<string>());
  const highlightData = useRef(highlights);
  highlightData.current = highlights;
  function syncHighlights(view: Rendition, annotations: Annotation[]) {
    const next = new Set(annotations.filter(item => item.quote).map(item => item.locator));
    for (const locator of applied.current) {
      if (!next.has(locator)) view.annotations.remove(locator, 'highlight');
    }
    for (const locator of next) {
      if (!applied.current.has(locator)) view.annotations.highlight(locator, {}, undefined, 'polka-highlight', {
        fill: '#e8b94d', 'fill-opacity': '.44',
      });
    }
    applied.current = next;
  }
  useEffect(() => {
    let live = true;
    let book: ReturnType<typeof ePub> | undefined;
    let current: Rendition | undefined;
    const abort = new AbortController();
    async function open() {
      let bytes: ArrayBuffer;
      if (content) bytes = await content.arrayBuffer();
      else {
        const res = await fetch('/api/v1/files/' + fileId + '?inline=true', forUser(userId,{signal:abort.signal}));
        if (!res.ok) throw new Error(t("Не удалось открыть EPUB"));
        bytes = await res.arrayBuffer();
      }
      if (!live || !element.current) return;
      book = ePub(bytes, {replacements: 'blobUrl'});
      current = book.renderTo(element.current, {width: '100%', height: '100%', spread: 'none',
        flow: flow ? 'scrolled-doc' : 'paginated', allowScriptedContent: false});
      rendition.current = current;
      current.on('selected', (locator: string, contents: {window: Window}) => {
        const quote = contents.window.getSelection()?.toString().trim().slice(0, 4000);
        if (quote && locator.length <= 4000) onSelection({locator, quote});
      });
      syncHighlights(current, highlightData.current);
      current.hooks.content.register((content: {document: Document}) => {
        const meta = content.document.createElement('meta');
        meta.httpEquiv = 'Content-Security-Policy';
        meta.content = "default-src 'none'; img-src blob: data:; font-src blob: data:; style-src 'unsafe-inline' blob:; media-src blob: data:";
        content.document.head.prepend(meta);
      });
      applyAppearance(current, appearance.current);
      const view = current;
      const selectBook = book;
      let starts: ReturnType<typeof chapterStarts> = [];
      let latest: Location | undefined;
      function report(location: Location) {
        if (!live) return;
        latest = location;
        const {page, total} = location.start.displayed;
        const section = location.start.index + (page - 1) / Math.max(1, total);
        const percentage = location.atEnd ? 1 : location.start.percentage ??
          (section / Math.max(1, (selectBook.spine as unknown as {length?: number}).length || 1));
        update({locator: location.start.cfi, percentage: Math.max(0, Math.min(1, percentage)),
          chapter: chapterAt(view, starts, location.start)});
      }
      view.on('relocated', report);
      void selectBook.loaded.navigation.then(navigation => {
        if (!live) return;
        starts = chapterStarts(selectBook, navigation.toc);
        onChapters(navigation.toc);
        if (latest) report(latest);
      }).catch(() => {});
      register({previous: () => void view.prev(), next: () => void view.next(),
        go: href => void view.display(href).catch(() => onError(t("Не удалось перейти к позиции"))),
        approximate: async value => {
          if (!selectBook.locations.length()) await selectBook.locations.generate(1000);
          const where = selectBook.locations.cfiFromPercentage(value);
          if (typeof where === 'string') await view.display(where);
        },
        search: async phrase => {
          const sections: Section[] = [];
          selectBook.spine.each((section: Section) => {sections.push(section);});
          const matches: SearchHit[] = [];
          for (const section of sections) {
            if (!live || matches.length >= 50) break;
            await section.load(selectBook.load.bind(selectBook));
            const found = section.find(phrase) as unknown as SearchHit[];
            matches.push(...found.slice(0, 50 - matches.length));
          }
          return matches;
        }});
      try {
        await current.display(start);
      } catch {
        await current.display();
      }
    }
    void open().catch(error => {if (live) onError(message(error));});
    return () => {live = false; abort.abort(); register(null); current?.destroy(); book?.destroy(); rendition.current = undefined; applied.current.clear();};
  }, [fileId, userId, content, start, flow, update, register, onError, onChapters, onSelection]);
  useEffect(() => {
    if (rendition.current) syncHighlights(rendition.current, highlights);
  }, [highlights]);
  useEffect(() => {
    if (rendition.current) applyAppearance(rendition.current, {font,theme,family,spacing});
  }, [font, theme, family, spacing]);
  useEffect(() => {
    const target = element.current;
    if (!target) return;
    const observer = new ResizeObserver(([entry]) => {
      const {width,height} = entry.contentRect;
      if (width > 0 && height > 0) rendition.current?.resize(Math.floor(width),Math.floor(height));
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, []);
  return <div className="epub-canvas" data-margin={margin}><div className="epub-page" ref={element}/></div>;
}

type Controller = {
  previous: () => void; next: () => void; go: (where: string) => void;
  approximate?: (percentage: number) => Promise<void>; search?: (phrase: string) => Promise<SearchHit[]>;
};

function useReadingSession(fileId: number, userId: number, ready: boolean) {
  useEffect(() => {
    if (!ready) return;
    const session = Array.from(crypto.getRandomValues(new Uint8Array(16)), octet => octet.toString(16).padStart(2, '0')).join('');
    let previous = performance.now(), lastInput = previous, activeMillis = 0, sequence = 0;
    let visible = document.visibilityState === 'visible' && document.hasFocus();
    function sample() {
      const stamp = performance.now();
      const activeEnd = Math.min(stamp, lastInput + 120_000);
      if (visible) activeMillis += Math.max(0, Math.min(activeEnd - previous, 15_000));
      previous = stamp;
    }
    function activity() {
      sample();
      lastInput = performance.now();
    }
    function send(keepalive = false) {
      sample();
      const seconds = Math.min(45, Math.floor(activeMillis / 1000));
      activeMillis -= seconds * 1000;
      void api('/reading/sessions/' + fileId, forUser(userId,{...json('PUT', {
        session_id: session, sequence: sequence++, active_seconds: seconds,
      }), keepalive})).catch(() => {});
    }
    function visibility() {
      sample();
      const before = visible;
      visible = document.visibilityState === 'visible' && document.hasFocus();
      if (visible && !before) activity();
      if (before && !visible) send(true);
    }
    const leave = () => send(true);
    const events = ['pointerdown', 'keydown', 'wheel'] as const;
    events.forEach(event => window.addEventListener(event, activity, {passive: true}));
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('focus', visibility);
    window.addEventListener('blur', visibility);
    window.addEventListener('pagehide', leave);
    const sampleTimer = setInterval(sample, 10_000);
    const sendTimer = setInterval(() => {if (visible) send();}, 30_000);
    send();
    return () => {
      clearInterval(sampleTimer);
      clearInterval(sendTimer);
      events.forEach(event => window.removeEventListener(event, activity));
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('focus', visibility);
      window.removeEventListener('blur', visibility);
      window.removeEventListener('pagehide', leave);
      leave();
    };
  }, [fileId, userId, ready]);
}

function AnnotationPanel({items, selection, point, cancelSelection, go, save, busy}: {
  items: Annotation[]; selection?: Selection; point?: Point; cancelSelection: () => void;
  go: (locator: string) => void; save: (verb: string, id?: number, data?: object) => Promise<boolean>; busy: boolean;
}) {
  const [editing, setEditing] = useState<number>();
  return <section className="panel">
    <h2>{t("Заметки и выделения")}</h2>
    {selection && <blockquote>{selection.quote}</blockquote>}
    <form className="fields" onSubmit={async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const note = String(new FormData(form).get('note')).trim();
      const locator = selection?.locator || point?.locator;
      if (!locator || (!selection && !note)) return;
      if (await save('POST', undefined, {locator, quote: selection?.quote || '', note})) {
        form.reset(); cancelSelection();
      }
    }}>
      <label>{t("Текст заметки")}<textarea name="note" maxLength={10000} required={!selection}/></label>
      <div className="actions">
        <button disabled={busy || (!selection && !point)}>{selection ? t("Сохранить выделение") : t("Сохранить заметку")}</button>
        {selection && <button type="button" onClick={cancelSelection}>{t("Снять выделение")}</button>}
      </div>
    </form>
    <ul className="rows">{items.map(item => <li key={item.id}>
      <div>{item.quote && <blockquote>{item.quote}</blockquote>}
        {editing !== item.id && item.note && <p className="saved-note">{item.note}</p>}
        {editing === item.id && <form className="fields" onSubmit={async event => {
          event.preventDefault();
          const value = new FormData(event.currentTarget).get('note');
          if (await save('PATCH', item.id, {note: value})) setEditing(undefined);
        }}>
          <label>{t("Изменить заметку")}<textarea name="note" maxLength={10000} required={!item.quote} defaultValue={item.note}/></label>
          <div className="actions"><button disabled={busy}>{t("Сохранить правку")}</button>
          <button type="button" onClick={() => setEditing(undefined)}>{t("Отмена")}</button></div>
        </form>}
      </div>
      <div className="actions"><button onClick={() => go(item.locator)}>{t("К месту")}</button>
        <button onClick={() => setEditing(item.id)}>{t("Править")}</button>
        <button disabled={busy} onClick={() => {void save('DELETE', item.id);}}>{t("Удалить")}</button></div>
    </li>)}</ul>
  </section>;
}

function PdfView({fileId, userId, content, start, update, register, zoom, onError, needle, onFound}: {
  fileId: number; userId: number; content?: Blob; start?: string; update: (point: Point) => void; register: (controller: Controller | null) => void;
  zoom: number; onError: (error: string) => void; needle: {text: string; serial: number} | undefined;
  onFound: (message: string) => void;
}) {
  const [document, setDocument] = useState<PDFDocumentProxy>();
  const [page, setPage] = useState(Number(start) || 1);
  const [textMode, setTextMode] = useState(false);
  const [pageText, setPageText] = useState<string>();
  useEffect(() => {
    setPageText(undefined);
    if (!textMode || !document) return;
    let active = true;
    void document.getPage(page).then(data => data.getTextContent()).then(content => {
      if (active) setPageText(content.items.map(item => 'str' in item ? item.str + (item.hasEOL ? '\n' : ' ') : '').join('').trim());
    }).catch(error => {if (active) onError(message(error));});
    return () => {active = false;};
  }, [document, page, textMode, onError]);
  const currentPage = useRef(page);
  currentPage.current = page;
  const canvas = useRef<HTMLCanvasElement>(null);
  const pane = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {pane.current?.scrollTo(0, 0);}, [page]);
  useEffect(() => {
    const observer = new ResizeObserver(entries => setWidth(entries[0].contentRect.width));
    if (pane.current) observer.observe(pane.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let live = true;
    let task: ReturnType<typeof getDocument> | undefined;
    async function load() {
      const source = content ? {data:await content.arrayBuffer()} : {url:'/api/v1/files/' + fileId + '?inline=true', httpHeaders:{'X-Polka-User':String(userId)}};
      if (!live) return;
      task = getDocument(source);
      const result = await task.promise;
      if (!live) return;
      setDocument(result); setPage(value => Math.min(value, result.numPages));
    }
    void load().catch(error => {if (live) onError(message(error));});
    return () => {live = false; void task?.destroy();};
  }, [fileId, userId, content, onError]);
  useEffect(() => {
    if (!document) return;
    const limit = (position: number) => Math.min(document.numPages, Math.max(1, position));
    register({previous: () => setPage(p => limit(p-1)), next: () => setPage(p => limit(p+1)),
      go: value => {if (Number(value) > 0) setPage(limit(Number(value)));},
      approximate: async percentage => {
        const target = limit(Math.ceil(percentage * document.numPages));
        if (currentPage.current === target) update({locator: String(target), percentage: target / document.numPages});
        else setPage(target);
      }});
    return () => register(null);
  }, [document, register, update]);
  useEffect(() => {
    if (document) update({locator: String(page), percentage: page / document.numPages});
  }, [document, page, update]);
  useEffect(() => {
    if (!document || !canvas.current) return;
    let active = true;
    let rendering: ReturnType<Awaited<ReturnType<PDFDocumentProxy['getPage']>>['render']> | undefined;
    const element = canvas.current;
    void document.getPage(page).then(data => {
      if (!active) return;
      const natural = data.getViewport({scale: 1});
      const view = data.getViewport({scale: Math.min(4, (Math.max(100, width - 20) / natural.width) * zoom)});
      const density = Math.min(window.devicePixelRatio, Math.sqrt(8_000_000 / (view.width * view.height)),
        8192 / Math.max(view.width, view.height));
      element.style.width = `${view.width}px`;
      element.style.height = `${view.height}px`;
      element.width = Math.max(1, Math.floor(view.width * density));
      element.height = Math.max(1, Math.floor(view.height * density));
      rendering = data.render({canvas: element, viewport: view,
        transform: [element.width / view.width, 0, 0, element.height / view.height, 0, 0]});
      return rendering.promise;
    }).catch(error => {if (active) onError(message(error));});
    return () => {active = false; rendering?.cancel();};
  }, [document, page, width, zoom, onError]);
  useEffect(() => {
    if (!document || !needle) return;
    const pdf = document;
    const text = needle.text.toLocaleLowerCase();
    let active = true;
    const timer = async () => {
      for (let index=1; index<=pdf.numPages && active; index++) {
        const pdfPage = await pdf.getPage(index);
        const contents = await pdfPage.getTextContent();
        if (!active) return;
        const pageText = contents.items.map(item => 'str' in item ? item.str : '').join(' ').toLocaleLowerCase();
        if (pageText.includes(text)) {
          setPage(index);
          onFound(t("Найдено на странице ") + index);
          return;
        }
      }
      if (active) onFound(t("В текстовом слое совпадений нет"));
    };
    void timer().catch(error => {if (active) onError(message(error));});
    return () => {active = false;};
  }, [document, needle, onFound, onError]);
  return <><button type="button" aria-pressed={textMode} onClick={() => setTextMode(!textMode)}>{t("Текстовый вид PDF")}</button>
    <div className="pdf-canvas" ref={pane}>
      <canvas hidden={textMode} aria-label={t("Страница ") + page} ref={canvas}/>
      {textMode && <article tabIndex={0} aria-label={t("Страница ") + page} className="pdf-text" style={{fontSize:zoom+'em'}}>
        <h2 aria-live="polite">{t("Страница ") + page}</h2>
        <p>{pageText === undefined ? t("Загрузка…") : pageText || t("На этой странице нет текстового слоя. Для чтения скана требуется OCR.")}</p>
      </article>}
    </div></>;
}

function ReadingSession({file, user, initial, offline, copy}: {file: Info; user: User; initial: Position; offline: boolean; copy?: SavedBook}) {
  const bookOrigin = useLocation().state?.bookOrigin;
  const client = useQueryClient();
  const progress = useProgress(file.id, initial, user);
  const content = useRef(copy?.content).current;
  const [savingCopy,setSavingCopy] = useState(false);
  const supported = offlineSupported();
  const local = useQuery({queryKey:['offline-copy',user.id,file.id], networkMode:'always', enabled:supported,
    queryFn:async () => Boolean(await savedBook(user.id,file.id))});
  const [theme, setTheme] = useState<Theme>('light');
  const [font, setFont] = useState(112);
  const [family,setFamily] = useState<FontFamily>('serif');
  const [spacing,setSpacing] = useState('1.65');
  const [margin,setMargin] = useState<Margin>('narrow');
  const [flow, setFlow] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState('');
  const [found, setFound] = useState('');
  const [needle, setNeedle] = useState<{text: string; serial: number}>();
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [ready, setReady] = useState(false);
  const [flowStart, setFlowStart] = useState(progress.start);
  const [marksOpen, setMarksOpen] = useState(false);
  const [annotationsOpen, setAnnotationsOpen] = useState(false);
  const [annotating, setAnnotating] = useState(false);
  const [selection, setSelection] = useState<Selection>();
  const hasSuggestion = Boolean(initial.koreader || initial.sony || initial.alternate);
  const [nativeVisible, setNativeVisible] = useState(hasSuggestion);
  const holdingPosition = useRef(hasSuggestion);
  const renderedPosition = useRef<Point | undefined>(undefined);
  const [jumping, setJumping] = useState(false);
  const controller = useRef<Controller | null>(null);
  const updatePosition = useCallback((value: Point) => {
    renderedPosition.current = value;
    if (!holdingPosition.current) progress.update(value);
  }, [progress.update]);
  function dismissExternalPosition() {
    holdingPosition.current = false;
    if (!initial.alternate && renderedPosition.current) progress.update(renderedPosition.current);
    setNativeVisible(false);
  }
  const region = useRef<HTMLElement>(null);
  const register = useCallback((next: Controller | null) => {controller.current = next; setReady(Boolean(next));}, []);
  const marks = useQuery({queryKey: ['bookmarks', user.id, file.id], queryFn: async () => {
    const result = await api<Mark[]>('/reading/bookmarks/' + file.id,forUser(user.id));
    void refreshSavedReading(user.id,file.id,{marks:result}).catch(() => {});
    return result;
  }, initialData:copy?.marks, enabled: marksOpen && !offline});
  const pdf = file.format === 'pdf';
  const annotations = useQuery({queryKey: ['annotations', user.id, file.id], queryFn: async () => {
    const result = await api<Annotation[]>('/reading/annotations/' + file.id,forUser(user.id));
    void refreshSavedReading(user.id,file.id,{annotations:result}).catch(() => {});
    return result;
  }, initialData:copy?.annotations, enabled: (!pdf || annotationsOpen) && !offline});
  const selectText = useCallback((item: Selection) => {setSelection(item); setAnnotationsOpen(true);}, []);
  useReadingSession(file.id, user.id, ready && !offline);
  useEffect(() => {
    const keys = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || /INPUT|SELECT|TEXTAREA|BUTTON/.test((event.target as HTMLElement).tagName)) return;
      if (event.key === 'ArrowLeft') controller.current?.previous();
      if (event.key === 'ArrowRight') controller.current?.next();
    };
    window.addEventListener('keydown', keys);
    return () => window.removeEventListener('keydown', keys);
  }, []);
  async function manageMark(method: string, mark?: number, label?: string) {
    try {
      await api('/reading/bookmarks/' + file.id + (mark ? '/' + mark : ''), forUser(user.id,json(method,
        label && progress.point ? {label, locator: progress.point.locator} : undefined)));
      await marks.refetch();
      return true;
    } catch (e) {setError(message(e)); return false;}
  }
  async function manageAnnotation(verb: string, id?: number, data?: object) {
    setAnnotating(true);
    setError('');
    try {
      await api('/reading/annotations/' + file.id + (id ? '/' + id : ''), forUser(user.id,json(verb, data)));
      await annotations.refetch();
      return true;
    } catch (e) {setError(message(e)); return false;} finally {setAnnotating(false);}
  }
  return <section className="reader" data-theme={theme} ref={region}>
    <header className="reader-heading"><Link className="back" state={offline ? undefined : bookOrigin} to={offline ? '/offline' : '/books/' + file.work_id}>
      <ChevronLeft size={16}/> {offline ? t("Книги без сети") : t("К книге")}</Link><h1>{file.title}</h1></header>
    <div className="reader-tools">
      <div className="actions">
        <button disabled={!ready} onClick={() => controller.current?.previous()} aria-label={t("Предыдущая страница")}><ChevronLeft size={18}/></button>
        <button disabled={!ready} onClick={() => controller.current?.next()} aria-label={t("Следующая страница")}><ChevronRight size={18}/></button>
      </div>
      <details className="reader-options" open={window.matchMedia('(min-width: 861px)').matches}>
        <summary>{t("Инструменты чтения")}</summary><div className="actions">
        <button aria-label={pdf ? t("Уменьшить масштаб") : t("Уменьшить шрифт")} onClick={() => pdf ? setZoom(v => Math.max(.5, v-.15)) : setFont(v => Math.max(75, v-10))}><Minus size={17}/></button>
        <button aria-label={pdf ? t("Увеличить масштаб") : t("Увеличить шрифт")} onClick={() => pdf ? setZoom(v => Math.min(3, v+.15)) : setFont(v => Math.min(225, v+10))}><Plus size={17}/></button>
        {!pdf && <select aria-label={t("Режим чтения")} value={flow ? 'scroll' : 'page'} onChange={event => {
          setFlowStart(progress.point?.locator); setFlow(event.target.value === 'scroll');
        }}><option value="page">{t("Страницы")}</option><option value="scroll">{t("Прокрутка")}</option></select>}
        {!pdf && <select aria-label={t("Тема")} value={theme} onChange={event => setTheme(event.target.value as Theme)}>
          <option value="light">{t("Светлая")}</option><option value="sepia">{t("Сепия")}</option><option value="dark">{t("Тёмная")}</option>
        </select>}
        {chapters.length > 0 && <select aria-label={t("Содержание")} value="" onChange={event => controller.current?.go(event.target.value)}>
          <option value="" disabled>{t("Содержание")}</option>{flattenChapters(chapters).map(({chapter, depth}, index) =>
            <option key={index} value={chapter.href}>{'\u00a0'.repeat(depth * 2) + chapter.label}</option>)}
        </select>}
        <button aria-expanded={marksOpen} onClick={() => setMarksOpen(!marksOpen)}><BookmarkIcon size={17}/>{t(" Закладки")}</button>
        <button aria-expanded={annotationsOpen} onClick={() => setAnnotationsOpen(!annotationsOpen)}>{t("Заметки")}</button>
        {supported && !offline && !local.data && <button disabled={savingCopy || local.isPending} onClick={async () => {
          setSavingCopy(true); setError('');
          try {
            await saveBook(user.id,file);
            await Promise.all([local.refetch(),client.invalidateQueries({queryKey:['offline-files',user.id]})]);
          } catch (e) {setError(message(e));} finally {setSavingCopy(false);}
        }}>{savingCopy ? t("Сохраняем копию…") : t("Сохранить без сети")}</button>}
        {supported && !offline && local.data && <Link className="button" to="/offline">{t("Копия сохранена")}</Link>}
        {!supported && !window.isSecureContext && <small>{t("Для сохранения без сети откройте сервер через HTTPS.")} <Link to="/offline">{t("Книги без сети")}</Link></small>}
        {document.fullscreenEnabled && <button aria-label={t("Полноэкранный режим")} onClick={() => {
          const action = document.fullscreenElement ? document.exitFullscreen() : region.current?.requestFullscreen();
          void action?.catch(e => setError(message(e)));
        }}><Maximize size={17}/></button>}
      </div>
      {!pdf && <details className="reader-typography"><summary>{t("Шрифт и поля")}</summary><div className="actions">
        <label>{t("Шрифт")}<select aria-label={t("Шрифт")} value={family} onChange={event => setFamily(event.target.value as FontFamily)}>
          <option value="serif">{t("С засечками")}</option><option value="sans">{t("Без засечек")}</option><option value="mono">{t("Моноширинный")}</option>
        </select></label>
        <label>{t("Межстрочный интервал")}<select aria-label={t("Межстрочный интервал")} value={spacing} onChange={event => setSpacing(event.target.value)}>
          <option value="1.35">{t("Плотный")}</option><option value="1.65">{t("Обычный")}</option><option value="2">{t("Свободный")}</option>
        </select></label>
        <label>{t("Поля")}<select aria-label={t("Поля")} value={margin} onChange={event => setMargin(event.target.value as Margin)}>
          <option value="narrow">{t("Узкие")}</option><option value="normal">{t("Обычные")}</option><option value="wide">{t("Широкие")}</option>
        </select></label>
      </div></details>}
      <form className="filters" onSubmit={async event => {
        event.preventDefault();
        const text = String(new FormData(event.currentTarget).get('needle')).trim();
        if (!text) return;
        setHits([]);
        setFound(t("Ищем…"));
        if (pdf) {
          setNeedle({text, serial: Date.now()});
        } else if (controller.current?.search) {
          setSearching(true);
          try {
            const matches = await controller.current.search(text);
            setHits(matches);
            setFound(matches.length ? t("Совпадений: ") + matches.length : t("Совпадений нет"));
          } catch (e) {
            setFound('');
            setError(message(e));
          } finally {setSearching(false);}
        }
      }}><input type="search" name="needle" aria-label={t("Поиск по тексту книги")} placeholder={t("Текст в книге")} required maxLength={120}/>
        <button disabled={!ready || searching}>{t("Найти")}</button></form>
      {found && <small role="status">{found}</small>}
      {hits.length > 0 && <details open>
        <summary>{t("Результаты поиска")}</summary>
        <ul className="rows search-hits">{hits.map(hit => <li key={hit.cfi}>
          <button className="text-button" onClick={() => controller.current?.go(hit.cfi)}>{hit.excerpt}</button>
        </li>)}</ul>
      </details>}
      </details>
    </div>
    {error && <Notice error>{error}</Notice>}
    {nativeVisible && initial.alternate && <div className="notice">
      <p>{t("В другом файле этой книги ({format}) прочитано {percent}%. Переход будет приблизительным.", {
        format: initial.alternate.format.toUpperCase(), percent: Math.round(initial.alternate.percentage * 100),
      })}</p>
      <div className="actions"><button disabled={!ready || jumping} onClick={async () => {
        const approximate = controller.current?.approximate;
        if (!approximate || !initial.alternate) return;
        setJumping(true);
        holdingPosition.current = false;
        try {
          await approximate(initial.alternate.percentage);
          setNativeVisible(false);
        } catch (e) {holdingPosition.current = true; setError(message(e));} finally {setJumping(false);}
      }}>{jumping ? t("Переходим…") : t("Перейти примерно")}</button>
      <button onClick={dismissExternalPosition}>{t("Оставить текущую")}</button></div>
    </div>}
    {nativeVisible && initial.koreader && <div className="notice">
      <p>{t("Позиция из KOReader: ")}{Math.round(initial.koreader.percentage * 100)}%.</p>
      <div className="actions"><button disabled={!ready || jumping} onClick={async () => {
        const native = initial.koreader!;
        setJumping(true);
        holdingPosition.current = false;
        try {
          if (pdf && /^[0-9]+$/.test(native.locator)) controller.current?.go(native.locator);
          else if (controller.current?.approximate) await controller.current.approximate(native.percentage);
          else throw new Error(t("Позиция KOReader недоступна для этого файла"));
          setNativeVisible(false);
        } catch (e) {holdingPosition.current = true; setError(message(e));} finally {setJumping(false);}
      }}>{jumping ? t("Переходим…") : pdf ? t("Перейти к странице") : t("Перейти примерно")}</button>
      <button onClick={dismissExternalPosition}>{t("Оставить текущую")}</button></div>
    </div>}
    {nativeVisible && initial.sony && <div className="notice">
      <p>{t("Последняя отметка Sony PRS-600")}{initial.sony.page && t(" · Страница ") + initial.sony.page}
        {initial.sony.percentage !== null && ' · ' + Math.round(initial.sony.percentage * 100) + '%'}.
      </p>
      <div className="actions">
        {(initial.sony.exact_page && initial.sony.page || !pdf && initial.sony.percentage !== null) &&
          <button disabled={!ready || jumping} onClick={async () => {
            const sony = initial.sony!;
            setJumping(true);
            holdingPosition.current = false;
            try {
              if (sony.exact_page && sony.page) controller.current?.go(String(sony.page));
              else if (sony.percentage !== null) await controller.current?.approximate?.(sony.percentage);
              setNativeVisible(false);
            } catch (e) {holdingPosition.current = true; setError(message(e));} finally {setJumping(false);}
          }}>{jumping ? t("Переходим…") : initial.sony.exact_page && initial.sony.page ? t("Перейти к странице") : t("Перейти примерно")}</button>}
        <button onClick={dismissExternalPosition}>{t("Оставить текущую")}</button>
      </div>
    </div>}
    {progress.conflict && <div className="notice" role="alert">
      <p>{t("На другом устройстве сохранено ")}{Math.round((progress.conflict.percentage || 0) * 100)}{t("%. Какую позицию оставить?")}</p>
      <div className="actions"><button onClick={() => progress.resolve(true, () => {})}>{t("Оставить мою")}</button>
      <button onClick={() => progress.resolve(false, locator => controller.current?.go(locator))}>{t("Перейти к позиции сервера")}</button></div>
    </div>}
    {marksOpen && <section className="panel">
      <h2>{t("Закладки")}</h2>
      {!offline && <form className="filters" onSubmit={async event => {
        event.preventDefault(); const form = event.currentTarget; const label = String(new FormData(form).get('label'));
        if (await manageMark('POST', undefined, label)) form.reset();
      }}><input name="label" aria-label={t("Название закладки")} placeholder={t("Название закладки")} required maxLength={200}/>
        <button disabled={!progress.point}>{t("Добавить")}</button></form>}
      <ul className="rows">{marks.data?.map(mark => <li key={mark.id}>
        <button className="text-button" onClick={() => controller.current?.go(mark.locator)}>{mark.label}</button>
        {!offline && <button onClick={() => manageMark('DELETE', mark.id)}>{t("Удалить")}</button>}
      </li>)}</ul>
      {marks.error && <Notice error>{message(marks.error)}</Notice>}
    </section>}
    {annotationsOpen && <>
      {annotations.error && <Notice error>{message(annotations.error)}</Notice>}
      <AnnotationPanel items={annotations.data || []} selection={selection} point={progress.point} busy={annotating || offline}
        cancelSelection={() => setSelection(undefined)} go={locator => controller.current?.go(locator)} save={manageAnnotation}/>
    </>}
    {pdf ? <PdfView fileId={file.id} userId={user.id} content={content} start={progress.start} update={updatePosition} register={register} zoom={zoom}
      onError={setError} needle={needle} onFound={setFound}/> :
      <EpubView fileId={file.id} userId={user.id} content={content} start={flowStart} update={updatePosition} register={register}
        font={font} theme={theme} family={family} spacing={spacing} margin={margin} flow={flow}
        onError={setError} onChapters={setChapters} onSelection={selectText}
        highlights={annotations.data || []}/>}
    <footer className="reader-footer"><span>{pdf && progress.point ? t("Страница ") + progress.point.locator + ' · ' : ''}
      {!pdf && progress.point?.chapter && progress.point.chapter + ' · '}
      {Math.round((progress.point?.percentage || 0) * 100)}%</span><span role="status">{progress.status}</span></footer>
  </section>;
}

type ReaderLaunch = {file: Info; position: Position; copy?: SavedBook; offline: boolean};

export default function Reader({user}: {user: User}) {
  const {id} = useParams();
  return <ReaderFile key={user.id + ':' + id} id={Number(id)} user={user}/>;
}

function ReaderFile({id, user}: {id: number; user: User}) {
  const [opened, setOpened] = useState(false);
  const launch = useQuery({queryKey:['reader-launch',user.id,id], networkMode:'always',
    refetchOnMount:'always', refetchOnReconnect:'always',
    queryFn: async (): Promise<ReaderLaunch> => {
      try {
        const expected = forUser(user.id);
        const [file,position] = await Promise.all([api<Info>('/files/' + id + '/info',expected),api<Position>('/reading/progress/' + id,expected)]);
        return {file,position,offline:false};
      } catch (error) {
        if (!(error instanceof TypeError) || !offlineSupported()) throw error;
        const copy = await savedBook(user.id,id);
        if (!copy) throw new Error(t("Книга недоступна без сети. Сохраните копию при подключении к Полке."));
        return {file:copy.file,position:copy.position,offline:true,copy};
      }
    }});
  useEffect(() => {
    if (launch.isFetchedAfterMount && !launch.isFetching && launch.data) setOpened(true);
  }, [launch.isFetchedAfterMount, launch.isFetching, launch.data]);
  if (launch.error) return <Notice error>{message(launch.error)}</Notice>;
  if (!opened || !launch.data) return <p>{t("Открываем книгу…")}</p>;
  const {file,position,copy,offline} = launch.data;
  if (!['epub','pdf'].includes(file.format)) return <Notice error>{t("Для чтения требуется EPUB или PDF.")}</Notice>;
  return <ReadingSession file={file} user={user} initial={position} copy={copy} offline={offline}/>;
}


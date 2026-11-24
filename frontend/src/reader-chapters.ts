import type Book from 'epubjs/types/book';
import type Contents from 'epubjs/types/contents';
import type Rendition from 'epubjs/types/rendition';
import type {DisplayedLocation} from 'epubjs/types/rendition';

export type Chapter = {href: string; label: string; subitems?: Chapter[]};

export function flattenChapters(chapters: Chapter[], depth = 0): {chapter: Chapter; depth: number}[] {
  return chapters.flatMap(chapter => [
    {chapter, depth},
    ...flattenChapters(chapter.subitems || [], depth + 1),
  ]);
}

type ChapterStart = {index: number; anchor: string; label: string};

export function chapterStarts(book: Book, toc: Chapter[]): ChapterStart[] {
  return flattenChapters(toc).flatMap(({chapter}) => {
    const label = Array.from(chapter.label.trim().replace(/\s+/g, ' ')).slice(0, 500).join('');
    const fragment = chapter.href.split('#', 2)[1] || '';
    try {
      const section = book.spine.get(chapter.href);
      return section && label ? [{index: section.index, anchor: decodeURIComponent(fragment), label}] : [];
    } catch {
      return [];
    }
  }).sort((a, b) => a.index - b.index);
}

export function chapterAt(view: Rendition, starts: ChapterStart[], location: DisplayedLocation): string | null {
  const contents = (view.getContents() as unknown as Contents[]).find(frame => frame.sectionIndex === location.index);
  let label: string | null = null;
  let nearest: string | undefined;
  for (const entry of starts) {
    if (entry.index > location.index) break;
    if (entry.index < location.index || !entry.anchor) {
      if (!nearest) label = entry.label;
      continue;
    }
    const target = contents?.document.getElementById(entry.anchor) || contents?.document.getElementsByName(entry.anchor)[0];
    if (!target || !contents) continue;
    const cfi = contents.cfiFromNode(target);
    if (view.epubcfi.compare(cfi, location.cfi) <= 0 && (!nearest || view.epubcfi.compare(cfi, nearest) >= 0)) {
      nearest = cfi;
      label = entry.label;
    }
  }
  return label;
}

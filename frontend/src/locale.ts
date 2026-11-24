import {useSyncExternalStore} from 'react';
import {english} from './locale.en';
import {serverEnglish} from './locale.server.en';

type Language = 'ru' | 'en';
const storageKey = 'polka.language';
const listeners = new Set<() => void>();

function savedLanguage(): Language {
  try {
    return localStorage.getItem(storageKey) === 'en' ? 'en' : 'ru';
  } catch {
    return 'ru';
  }
}

let language = savedLanguage();

export function currentLanguage() {
  return language;
}

export function setLanguage(value: Language) {
  if (language === value) return;
  language = value;
  try {
    localStorage.setItem(storageKey, value);
  } catch {
    // Keep the choice for the current tab when storage is blocked.
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useLanguage() {
  return useSyncExternalStore(subscribe, currentLanguage);
}

export function t(text: string, values?: Record<string, string | number>) {
  const key = text.trim();
  const start = text.indexOf(key);
  const englishText = Object.hasOwn(english, key) ? english[key]
    : Object.hasOwn(serverEnglish, key) ? serverEnglish[key] : undefined;
  const translated = language === 'en' && englishText
    ? text.slice(0, start) + englishText + text.slice(start + key.length)
    : text;
  return values ? translated.replace(/\{(\w+)\}/g, (match, name: string) => String(values[name] ?? match)) : translated;
}

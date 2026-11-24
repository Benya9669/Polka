import {setLanguage, t, useLanguage} from './locale';
import {useEffect, useState} from 'react';

type Theme = 'system' | 'light' | 'dark';
const key = 'polka.interfaceTheme';

function savedTheme(): Theme {
  try {
    const value = localStorage.getItem(key);
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    return 'system';
  }
}

export function useInterfaceTheme() {
  const [theme, setTheme] = useState(savedTheme);
  useEffect(() => {
    const preference = window.matchMedia('(prefers-color-scheme: dark)');
    function apply() {
      const resolved = theme === 'system' ? (preference.matches ? 'dark' : 'light') : theme;
      document.documentElement.dataset.interfaceTheme = resolved;
      document.querySelector('meta[name="theme-color"]')?.setAttribute('content', resolved === 'dark' ? '#222820' : '#f6f2e9');
    }
    apply();
    preference.addEventListener('change', apply);
    return () => preference.removeEventListener('change', apply);
  }, [theme]);

  function changeTheme(value: Theme) {
    setTheme(value);
    try {
      localStorage.setItem(key, value);
    } catch {
      // Keep the current choice for this tab if storage is unavailable.
    }
  }
  return {theme, changeTheme};
}

export function LanguageSelect() {
  const language = useLanguage();
  return <label>{t("Язык интерфейса")}<select value={language} onChange={event => {
    setLanguage(event.target.value === 'en' ? 'en' : 'ru');
  }}>
    <option value="ru" lang="ru">Русский</option>
    <option value="en" lang="en">English</option>
  </select></label>;
}

export function AppearanceSettings({theme, changeTheme}: ReturnType<typeof useInterfaceTheme>) {
  return <section className="panel fields appearance-settings">
    <h2>{t("Внешний вид")}</h2>
    <label>{t("Тема интерфейса")}<select value={theme} onChange={event => changeTheme(event.target.value as Theme)}>
      <option value="system">{t("Как в системе")}</option>
      <option value="light">{t("Светлая")}</option>
      <option value="dark">{t("Тёмная")}</option>
    </select></label>
    <LanguageSelect/>
  </section>;
}

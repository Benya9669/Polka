import {expect, test, type Page} from '@playwright/test';
import {createHash} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import JSZip from 'jszip';

function notifications(page: Page) {
  return page.getByRole('complementary', {name:'Уведомления'});
}

test('переключает интерфейс на английский и сохраняет язык при повторном входе', async ({page, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}})).ok()).toBeTruthy();
  const library = await request.post('/api/v1/libraries',{data:{name:'Язык ' + Date.now(),mode:'managed'}}).then(response => response.json());
  const upload = await request.post('/api/v1/libraries/' + library.id + '/upload',{multipart:{file:{name:'language.epub',
    mimeType:'application/epub+zip',buffer:await sample()}}});
  expect(upload.ok()).toBeTruthy();
  const info = await upload.json();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.setViewportSize({width:390,height:844});
  await page.goto('/settings');
  await expect(page.locator('html')).toHaveAttribute('lang','ru');
  await page.getByLabel('Язык интерфейса').selectOption('en');
  await expect(page.locator('html')).toHaveAttribute('lang','en');
  await expect(page).toHaveTitle('Polka — ebook library');
  await expect(page.getByRole('heading',{name:'Settings',exact:true})).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Display language')).toHaveValue('en');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.screenshot({path:testInfo.outputPath('settings-english-mobile.png')});

  await page.getByRole('link',{name:'Search sources',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Search sources',exact:true})).toBeVisible();
  await expect(page.getByRole('searchbox',{name:'Search all catalogs'})).toBeVisible();
  await page.goto('/read/' + info.file.id);
  await expect(page.getByLabel('Table of contents')).toBeVisible();
  await expect(page.getByRole('button',{name:'Save for offline reading'})).toBeVisible();
  await expect(page.frameLocator('.epub-canvas iframe').getByRole('heading',{name:'Глава 1'})).toBeVisible();

  await page.goto('/settings');
  await page.getByRole('button',{name:'Sign out',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Sign in',exact:true})).toBeVisible();
  await page.getByLabel('Username').fill('browserqa');
  await page.getByLabel('Password').fill('wrong-password');
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await expect(page.getByRole('alert')).toHaveText('Check your username and password');
  await page.getByLabel('Display language').selectOption('ru');
  await expect(page.locator('html')).toHaveAttribute('lang','ru');
  await expect(page.getByRole('heading',{name:'Вход',exact:true})).toBeVisible();
  await expect(page.getByRole('alert')).toHaveText('Проверьте имя и пароль');
});

test('сохраняет тему интерфейса и открывает поиск с клавиатуры', async ({page, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}})).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.emulateMedia({colorScheme:'dark'});
  await page.goto('/settings');
  const theme = page.getByLabel('Тема интерфейса');
  const html = page.locator('html');
  await expect(theme).toHaveValue('system');
  await expect(html).toHaveAttribute('data-interface-theme','dark');
  await theme.selectOption('light');
  await expect(html).toHaveCSS('color-scheme','light');
  await page.reload();
  await expect(theme).toHaveValue('light');
  await expect(html).toHaveAttribute('data-interface-theme','light');
  await theme.selectOption('dark');
  await page.emulateMedia({colorScheme:'light'});
  await expect(html).toHaveAttribute('data-interface-theme','dark');
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:testInfo.outputPath('interface-dark-mobile.png')});

  let releaseBooks: () => void = () => {};
  const pending = new Promise<void>(resolve => {releaseBooks = resolve;});
  await page.route(url => url.pathname === '/api/v1/books', async route => {
    await pending;
    await route.fulfill({json:{items:[],has_next:false}});
  });
  try {
    await page.getByRole('link',{name:'Все книги',exact:true}).click();
    await expect(page.locator('main').getByRole('status')).toHaveText('Загружаем каталог…');
    await expect(page.locator('.book-grid[aria-hidden=true] > li')).toHaveCount(6);
    await page.keyboard.press('Control+k');
    await expect(page.getByRole('searchbox',{name:'Поиск по метаданным'})).toBeFocused();
  } finally {
    releaseBooks();
  }
  await expect(page.getByRole('heading',{name:'Здесь появятся книги'})).toBeVisible();
});

test('предлагает объявленный способ входа и сохраняет источник', async ({page, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}})).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  const url = 'https://books.example/catalog';
  await page.route('**/api/v1/opds/sources/authentication', async route => {
    expect(route.request().postDataJSON()).toEqual({url});
    await route.fulfill({json:{secure:true,same_origin:true,protected:true,document:{
      title:'Каталог читательского клуба',description:'Используйте номер своего билета.',
      methods:[{type:'http://opds-spec.org/auth/basic',kind:'basic',login:'Номер билета',password:'Код доступа'}],
      links:{help:'https://books.example/help'},
    }}});
  });
  await page.setViewportSize({width:390,height:844});
  await page.goto('/sources');
  await page.getByRole('button',{name:'Добавить источник'}).click();
  await page.getByLabel('URL каталога').fill(url);
  await page.getByRole('button',{name:'Определить способ входа'}).click();
  await expect(page.locator('main').getByRole('status')).toHaveText('HTTP Basic');
  await expect(page.getByLabel('Авторизация')).toHaveValue('basic');
  await expect(page.getByRole('link',{name:'Помощь со входом'})).toHaveAttribute('href','https://books.example/help');
  await page.locator('.source-auth').screenshot({path:testInfo.outputPath('opds-auth-mobile.png')});
  await page.setViewportSize({width:1280,height:860});
  await page.locator('form').filter({hasText:'Новый источник'}).screenshot({path:testInfo.outputPath('opds-auth-desktop.png')});
  const name = 'Защищённый каталог ' + Date.now();
  await page.getByLabel('Название',{exact:true}).fill(name);
  await page.getByLabel('Номер билета').fill('reader');
  await page.getByLabel('Код доступа').fill('catalog-password');
  await page.clock.install();
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  const savedToast = notifications(page).getByRole('listitem').filter({hasText:'Источник сохранён'});
  await expect(savedToast).toBeVisible();
  await savedToast.getByRole('button',{name:'Закрыть уведомление'}).focus();
  await page.clock.fastForward(10_000);
  await expect(savedToast).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(savedToast).toHaveCount(0);
  const sources = await request.get('/api/v1/opds/sources').then(response => response.json());
  const saved = sources.find((source: {name:string}) => source.name === name);
  expect(saved).toMatchObject({url, authenticated:true});
  expect(JSON.stringify(saved)).not.toContain('catalog-password');
  expect((await request.delete('/api/v1/opds/sources/' + saved.id)).ok()).toBeTruthy();
});

test('импортирует FB2 из внешнего OPDS и продолжает чтение в другом сеансе', async ({page, browser, request, baseURL}, testInfo) => {
  const fixture = process.env.EBOOK_TEST_OPDS_FIXTURE;
  test.skip(!fixture, 'Требуется отдельный mock-сервер OPDS');
  test.setTimeout(75_000);
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const suffix = 'opds-' + Date.now();
  const directory = join(fixture!,suffix);
  await mkdir(directory);
  const fb2 = Buffer.from('<?xml version="1.0" encoding="UTF-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description>' +
    '<title-info><genre>sf</genre><author><first-name>Тестовый</first-name><last-name>автор</last-name></author>' +
    '<book-title>Полярная книга</book-title><lang>ru</lang></title-info></description><body><section><title><p>Глава</p></title>' +
    '<p>Проверка чтения из внешнего каталога.</p></section><section><title><p>Вторая глава</p></title>' +
    '<p>Проверка сохранённой позиции после импорта.</p></section></body></FictionBook>');
  const publication = {metadata:{title:'Полярная книга',identifier:'urn:test:' + suffix,
    author:{name:'Тестовый автор'},language:['ru'],belongsTo:{series:{name:'Северный цикл',position:2.5}}},
    links:[{href:'edition.fb2?token=test-signed-link',rel:'http://opds-spec.org/acquisition/open-access',
      type:'application/x-fictionbook+xml',length:fb2.length}]};
  function facet(sorted: boolean) {
    return [{metadata:{title:'Порядок книг'},links:[
      {href:'catalog.json',title:'По умолчанию',rel:sorted ? 'alternate' : 'self'},
      {href:'popular.json',title:'По популярности',rel:sorted ? 'self' : 'alternate',properties:{numberOfItems:1}},
    ]}];
  }
  await Promise.all([
    writeFile(join(directory,'edition.fb2'),fb2),
    writeFile(join(directory,'catalog.json'),JSON.stringify({metadata:{title:'Пробный каталог'},
      links:[{href:'catalog.json{?query}',rel:'search',type:'application/opds+json'}],
      publications:[publication],facets:facet(false)})),
    writeFile(join(directory,'popular.json'),JSON.stringify({metadata:{title:'Популярные книги'},
      links:[{href:'catalog.json',rel:'prev',type:'application/opds+json'}],publications:[publication],facets:facet(true)})),
  ]);
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}})).ok()).toBeTruthy();
  const library = await request.post('/api/v1/libraries',{data:{name:suffix,mode:'managed'}}).then(response => response.json());
  const upload = await request.post('/api/v1/libraries/' + library.id + '/upload',{multipart:{file:{name:'reader.epub',
    mimeType:'application/epub+zip',buffer:await sample()}}});
  expect(upload.ok()).toBeTruthy();
  const file = (await upload.json()).file;
  expect((await request.patch('/api/v1/libraries/' + library.id,{data:{formats:['fb2']}})).ok()).toBeTruthy();
  const before = await request.get('/api/v1/files/' + file.id + '/info').then(response => response.json());
  const created = await request.post('/api/v1/opds/sources',{data:{name:suffix,url:'http://polka-qa-opds:8090/' + suffix + '/catalog.json'}});
  expect(created.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.setViewportSize({width:390,height:844});
  await page.goto('/sources');
  await page.getByRole('link',{name:'Поиск по источникам',exact:true}).click();
  await expect(page).toHaveURL(/\/sources\/search$/);
  await page.getByRole('searchbox',{name:'Поиск во всех каталогах'}).fill('Полярная книга');
  await page.getByRole('button',{name:'Искать везде'}).click();
  await expect(page.getByRole('heading',{name:'Уже есть в библиотеке',exact:true})).toBeVisible({timeout:15000});
  const available = page.locator('.opds-results').filter({has:page.getByRole('heading',{name:'Можно скачать',exact:true})});
  const searchResult = available.locator('.panel').filter({has:page.getByRole('heading',{name:suffix,exact:true})});
  await expect(searchResult.locator('.opds-matches a[href="/books/' + before.work_id + '"]')).toHaveText('Полярная книга');
  await searchResult.screenshot({path:testInfo.outputPath('opds-search-mobile.png')});
  await page.getByRole('link',{name:'OPDS-каталоги',exact:true}).click();
  const open = () => page.locator('.source-list > li').filter({hasText:suffix}).getByRole('button',{name:'Открыть',exact:true}).click();
  await open();
  await expect(page.getByText('Северный цикл · Том 2.5')).toBeVisible();
  const order = page.getByRole('combobox',{name:'Порядок книг'});
  await expect(order.locator('option:checked')).toHaveText('По умолчанию');
  await order.selectOption({label:'По популярности · 1'});
  await expect(page.getByRole('heading',{name:'Популярные книги',exact:true})).toBeVisible();
  await expect(order.locator('option:checked')).toHaveText('По популярности · 1');
  await page.getByRole('button',{name:'Предыдущая страница',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Пробный каталог',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Скачать FB2 без импорта',exact:true}).click();
  await expect(page).toHaveURL(/\/downloads\?file=\d+$/);
  const queued = notifications(page).getByRole('listitem').filter({hasText:'Файл добавлен в очередь'});
  await expect(queued).toBeVisible();
  await queued.screenshot({path:testInfo.outputPath('opds-queued-mobile.png')});
  await queued.getByRole('button',{name:'Закрыть уведомление'}).click();
  const direct = new URL(page.url()).searchParams.get('file');
  const link = page.locator('a[href="/api/v1/opds/downloads/' + direct + '/file"]');
  await expect(link).toHaveAccessibleName('Скачать файл');
  const downloadRow = page.locator('.operation-list > li').filter({has:link});
  await expect(downloadRow.getByText('Файл готов',{exact:true})).toBeVisible();
  await downloadRow.screenshot({path:testInfo.outputPath('opds-file-mobile.png')});
  const [received] = await Promise.all([page.waitForEvent('download'),link.click()]);
  expect(received.suggestedFilename()).toBe('Полярная книга.fb2');
  expect(await readFile(await received.path())).toEqual(fb2);
  const unchanged = await request.get('/api/v1/books/' + before.work_id).then(response => response.json());
  expect(unchanged.editions).toHaveLength(1);
  await page.goto('/sources');
  await open();
  await page.getByLabel('Добавлять в').selectOption(String(library.id));
  await page.getByRole('button',{name:/Импорт FB2/}).click();
  const review = page.locator('#download-review');
  await review.getByLabel('Карточка для импорта').selectOption(String(before.work_id));
  await review.screenshot({path:testInfo.outputPath('opds-mobile.png')});
  await page.setViewportSize({width:1280,height:860});
  await review.screenshot({path:testInfo.outputPath('opds-desktop.png')});
  await review.getByRole('button',{name:'Импортировать и читать'}).click();
  await expect(page).toHaveURL(/\/read\/\d+$/,{timeout:25000});
  const toc = page.getByLabel('Содержание');
  const chapter = toc.getByRole('option',{name:'Вторая глава',exact:true});
  await expect(chapter).toHaveCount(1);
  await toc.selectOption((await chapter.getAttribute('value'))!);
  const resumedText = 'Проверка сохранённой позиции после импорта.';
  await expect(page.frameLocator('.epub-canvas iframe').getByText(resumedText)).toBeVisible();
  await expect(page.locator('.reader-footer').getByRole('status')).toContainText('Позиция сохранена');
  const readPath = new URL(page.url()).pathname;
  const otherSession = await browser.newContext({viewport:{width:820,height:1180},hasTouch:true,isMobile:true});
  try {
    const tablet = await otherSession.newPage();
    await tablet.goto('/home');
    await tablet.getByLabel('Имя пользователя').fill('browserqa');
    await tablet.getByLabel('Пароль').fill('browser-qa-password');
    await tablet.getByRole('button',{name:'Войти',exact:true}).click();
    const continueReading = tablet.locator('.home-section').filter({has:tablet.getByRole('heading',{name:'Продолжить чтение',exact:true})});
    await continueReading.locator(`a[href="${readPath}"]`).first().click();
    await expect(tablet.frameLocator('.epub-canvas iframe').getByText(resumedText)).toBeVisible();
    await expect(tablet.locator('.reader-footer')).toContainText('Вторая глава');
  } finally {
    await otherSession.close();
  }
  const after = await request.get('/api/v1/books/' + before.work_id).then(response => response.json());
  expect(after.editions).toHaveLength(2);
  const downloads = await request.get('/api/v1/opds/downloads').then(response => response.json());
  const acquired = downloads.find((job: {library_id: number | null}) => job.library_id === library.id).result;
  expect(acquired.provenance.identifier).toBe('urn:test:' + suffix);
  expect(JSON.stringify(acquired)).not.toContain('test-signed-link');
  await page.goto('/downloads');
  const fileLink = page.locator('a[href="/api/v1/files/' + acquired.file_id + '"]');
  const importRow = page.locator('.operation-list > li').filter({has:fileLink});
  await expect(importRow.getByText('Добавлено в библиотеку',{exact:true})).toBeVisible();
});

test('открывает EPUB и PDF без сети после перезагрузки и отправляет позицию после подключения', async ({page, request, baseURL}, testInfo) => {
  test.setTimeout(75_000);
  page.setDefaultTimeout(8000);
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  const login = await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}});
  expect(login.ok()).toBeTruthy();
  const admin = await login.json();
  const library = await request.post('/api/v1/libraries',{data:{name:'Офлайн ' + Date.now(),mode:'managed',shared:true}}).then(response => response.json());
  async function upload(name: string, buffer: Buffer, mimeType: string) {
    const response = await request.post('/api/v1/libraries/' + library.id + '/upload',{multipart:{file:{name,mimeType,buffer}}});
    expect(response.ok()).toBeTruthy();
    return (await response.json()).file.id as number;
  }
  const epub = await upload('offline.epub',await sample(),'application/epub+zip');
  const pdf = await upload('offline-pdf.pdf',samplePdf(),'application/pdf');
  await page.context().addCookies((await request.storageState()).cookies);
  const chapter = page.frameLocator('.epub-canvas iframe');
  await page.goto('/read/' + epub);
  await expect(chapter.getByRole('heading',{name:'Глава 1'})).toBeVisible();
  await page.getByText('Шрифт и поля',{exact:true}).click();
  await page.getByLabel('Шрифт',{exact:true}).selectOption('mono');
  await page.getByLabel('Межстрочный интервал',{exact:true}).selectOption('2');
  await page.getByLabel('Поля',{exact:true}).selectOption('wide');
  await expect(chapter.locator('body')).toHaveCSS('font-family','monospace');
  await expect.poll(() => chapter.locator('body').evaluate(node => {
    const style = getComputedStyle(node);
    return Math.round(parseFloat(style.lineHeight) / parseFloat(style.fontSize));
  })).toBe(2);
  await expect(page.locator('.epub-canvas')).toHaveAttribute('data-margin','wide');
  await expect(page.locator('.reader-footer').getByRole('status')).toContainText('Позиция сохранена');
  await page.getByRole('button',{name:'Сохранить без сети',exact:true}).click();
  await expect(page.getByRole('link',{name:'Копия сохранена',exact:true})).toBeVisible();
  await page.goto('/read/' + pdf);
  await expect(page.getByLabel('Страница 1',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Сохранить без сети',exact:true}).click();
  await expect(page.getByRole('link',{name:'Копия сохранена',exact:true})).toBeVisible();
  await page.context().setOffline(true);
  try {
    await page.goto('/offline');
    await expect(page.getByRole('heading',{name:'Книги без сети',exact:true})).toBeVisible();
    await page.locator('main').screenshot({path:testInfo.outputPath('offline-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await page.locator('main').screenshot({path:testInfo.outputPath('offline-mobile.png')});
    await page.getByRole('link',{name:'offline-pdf',exact:true}).click();
    await expect(page.getByLabel('Страница 1',{exact:true})).toBeVisible();
    await page.getByLabel('Поиск по тексту книги').fill('searchable');
    await page.getByRole('button',{name:'Найти',exact:true}).click();
    await expect(page.getByText('Найдено на странице 1')).toBeVisible();
    await page.goto('/read/' + epub);
    await expect(chapter.getByRole('heading',{name:'Глава 1'})).toBeVisible();
    await page.getByLabel('Содержание').selectOption('two.xhtml');
    await expect(chapter.getByRole('heading',{name:'Глава 2'})).toBeVisible();
    await expect(page.locator('.reader-footer').getByRole('status')).toContainText('Позиция сохранена в браузере');
    await page.reload();
    await expect(chapter.getByRole('heading',{name:'Глава 2'})).toBeVisible();
  } finally { await page.context().setOffline(false); }
  await expect(page.locator('.reader-footer').getByRole('status')).toHaveText('Позиция сохранена');
  const point = await request.get('/api/v1/reading/progress/' + epub).then(response => response.json());
  expect(point.version).toBeGreaterThanOrEqual(2);
  const alternate = {username:'offline-' + Date.now(),password:'reader-offline-password'};
  expect((await request.post('/api/v1/auth/users',{data:alternate})).ok()).toBeTruthy();
  expect((await page.request.post('/api/v1/auth/login',{data:alternate})).ok()).toBeTruthy();
  const oldTab = await page.request.put('/api/v1/reading/progress/' + epub,{
    headers:{'X-Polka-User':String(admin.id)},data:{version:0,locator:point.locator,percentage:.9},
  });
  expect(oldTab.status()).toBe(409);
  await page.goto('/offline');
  await expect(page.getByText('Сохранённых книг пока нет')).toBeVisible();
  const alternatePoint = await page.request.get('/api/v1/reading/progress/' + epub).then(response => response.json());
  expect(alternatePoint.version).toBe(0);
});

test('выбирает издания и пропуск прямо в плане импорта', async ({page, request, baseURL}, testInfo) => {
  const inbox = process.env.EBOOK_TEST_INBOX;
  test.skip(!inbox, 'Требуется отдельная папка ввода QA');
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const auth = await request.get('/api/v1/auth/status').then(res => res.json());
  expect((await request.post('/api/v1/auth/' + (auth.setup ? 'setup' : 'login'), {
    data:{username:'browserqa', password:'browser-qa-password'},
  })).ok()).toBeTruthy();
  const folder = 'plan-' + Date.now();
  const directory = join(inbox!, folder);
  await mkdir(directory);
  const book = await sample();
  const alternate = await JSZip.loadAsync(book);
  alternate.file('one.xhtml', (await alternate.file('one.xhtml')!.async('string')) + '\n<!-- второе издание -->');
  const skippedBook = '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info>' +
    '<book-title>Другая книга</book-title></title-info></description><body><section><p>Текст</p></section></body></FictionBook>';
  await Promise.all([writeFile(join(directory,'a.epub'),book),
    writeFile(join(directory,'b.epub'),await alternate.generateAsync({type:'nodebuffer'})),
    writeFile(join(directory,'broken.fb2'),'<FictionBook><broken'),
    writeFile(join(directory,'omit.fb2'),skippedBook)]);
  const library = await request.post('/api/v1/libraries',{data:{name:folder,mode:'managed'}}).then(res => res.json());
  const planned = await request.post('/api/v1/libraries/' + library.id + '/scan',{data:{folder}});
  expect(planned.ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.setViewportSize({width:390,height:844});
  await page.goto('/downloads');
  const plan = page.locator('.operation-list > li').filter({hasText:'План импорта'}).first();
  await expect(plan.getByText('Ошибки формата или метаданных: 1',{exact:true})).toBeVisible();
  await expect(plan.getByText('Будет создано карточек: 3')).toBeVisible();
  await expect(plan.getByText('Будет конвертировано файлов: 1')).toBeVisible();
  await plan.getByText('Файлы в плане (3)',{exact:true}).click();
  await plan.getByLabel('Действие для b.epub',{exact:true}).selectOption('p:a.epub');
  await plan.getByLabel('Действие для omit.fb2',{exact:true}).selectOption('skip');
  await expect(plan.getByText('Будет создано карточек: 1')).toBeVisible();
  await expect(plan.getByText('Будет конвертировано файлов: 0')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await plan.screenshot({path:testInfo.outputPath('import-mobile.png')});
  await page.setViewportSize({width:1280,height:860});
  await plan.screenshot({path:testInfo.outputPath('import-desktop.png')});
  await plan.getByRole('button',{name:'Подтвердить импорт',exact:true}).click();
  const catalog = '/api/v1/books?library_id=' + library.id;
  await expect.poll(async () => (await request.get(catalog).then(res => res.json())).items[0]?.editions.length).toBe(2);
  const result = await request.get(catalog).then(res => res.json());
  expect(result.items).toHaveLength(1);
  expect(result.items[0].editions).toHaveLength(2);
});

test('редактирует подсерии и показывает тома в порядке номеров', async ({page, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const auth = await request.get('/api/v1/auth/status').then(res => res.json());
  const login = await request.post('/api/v1/auth/' + (auth.setup ? 'setup' : 'login'), {
    data:{username:'browserqa', password:'browser-qa-password'},
  });
  expect(login.ok()).toBeTruthy();
  const suffix = Date.now();
  const lib = await request.post('/api/v1/libraries', {data:{name:'Библиотека серий ' + suffix, mode:'managed'}}).then(res => res.json());
  const root = 'Морские истории ' + suffix;
  const branch = 'Острова';
  async function record(title: string, position?: string) {
    const up = await request.post('/api/v1/libraries/' + lib.id + '/upload', {multipart:{file:{
      name:title + '.txt', mimeType:'text/plain', buffer:Buffer.from(title),
    }}});
    expect(up.ok()).toBeTruthy();
    const {file} = await up.json();
    const {work_id} = await request.get('/api/v1/files/' + file.id + '/info').then(res => res.json());
    if (position) {
      const modified = await request.put('/api/v1/books/' + work_id,{data:{title,authors:[],series_memberships:[{path:[root,branch],position}]}});
      expect(modified.ok()).toBeTruthy();
    }
    return work_id;
  }
  const title = 'Средний том';
  await record('Заключительный том', '10');
  await record('Начальный том', '2');
  const book = await record(title);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/books/' + book);
  await page.getByRole('button',{name:'Редактировать метаданные'}).click();
  await page.getByRole('button',{name:'Добавить серию',exact:true}).click();
  await page.getByLabel('Название серии',{exact:true}).fill(root);
  await page.getByRole('button',{name:'Добавить подсерию',exact:true}).click();
  await page.getByLabel('Название подсерии',{exact:true}).fill(branch);
  await page.getByLabel('Номер тома',{exact:true}).fill('2.5');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await expect(notifications(page).getByText('Метаданные сохранены', {exact:true})).toBeVisible();
  await page.getByRole('link',{name:root + ' / ' + branch,exact:true}).click();
  const volumes = page.getByRole('region',{name:'Тома',exact:true});
  await expect(volumes.getByRole('link')).toHaveText(['Начальный том',title,'Заключительный том']);
  await page.locator('main').screenshot({path:testInfo.outputPath('series-desktop.png')});
  await page.setViewportSize({width:390,height:844});
  await page.locator('main').screenshot({path:testInfo.outputPath('series-mobile.png')});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('link',{name:root,exact:true}).click();
  await expect(page.getByRole('region',{name:'Подсерии',exact:true}).getByRole('link',{name:branch,exact:true})).toBeVisible();
});

test('находит автора по псевдониму и связывает ранее импортированные книги', async ({page, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const auth = await request.get('/api/v1/auth/status').then(res => res.json());
  const login = await request.post('/api/v1/auth/' + (auth.setup ? 'setup' : 'login'), {
    data:{username:'browserqa', password:'browser-qa-password'},
  });
  expect(login.ok()).toBeTruthy();
  const run = Date.now();
  const lib = await request.post('/api/v1/libraries', {data:{name:'Авторы ' + run, mode:'managed'}}).then(res => res.json());
  async function record(title: string, creator: string) {
    const upload = await request.post('/api/v1/libraries/' + lib.id + '/upload', {multipart:{file:{
      name:title + '.txt', mimeType:'text/plain', buffer:Buffer.from(title),
    }}});
    expect(upload.ok()).toBeTruthy();
    const {file} = await upload.json();
    const {work_id} = await request.get('/api/v1/files/' + file.id + '/info').then(res => res.json());
    const edited = await request.put('/api/v1/books/' + work_id, {data:{title, authors:[creator]}});
    expect(edited.ok()).toBeTruthy();
    return {workId:work_id, authorId:(await edited.json()).author_credits[0].id};
  }
  const first = 'Письма ' + run;
  const second = 'Дневник ' + run;
  const name = 'Алла Лесная ' + run;
  const alias = 'Лада Норд ' + run;
  const original = await record(first, name);
  const alternate = await record(second, alias);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/books/' + original.workId);
  await page.getByRole('link', {name, exact:true}).click();
  await expect(page.getByRole('heading', {name, exact:true})).toBeVisible();
  await page.getByLabel('Добавить псевдоним').fill(alias);
  await page.getByRole('button', {name:'Добавить', exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('уже есть профиль');
  await page.getByRole('button', {name:'Объединить с профилем «' + alias + '»'}).click();
  await expect(notifications(page).getByText('Профили объединены.', {exact:true})).toBeVisible();
  await expect(page.getByRole('link', {name:new RegExp(second)})).toBeVisible();
  await page.locator('main').screenshot({path:testInfo.outputPath('authors-desktop.png')});
  await page.setViewportSize({width:390, height:844});
  await page.locator('main').screenshot({path:testInfo.outputPath('authors-mobile.png')});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.goto('/authors/' + alternate.authorId);
  await expect(page).toHaveURL('/authors/' + original.authorId);
  await page.getByRole('link', {name:'Все авторы', exact:true}).click();
  await page.getByLabel('Имя или псевдоним').fill(alias);
  await page.getByRole('button', {name:'Найти', exact:true}).click();
  await expect(page.getByRole('link', {name, exact:true})).toBeVisible();
});

test('объединяет карточки после предпросмотра и открывает прежнюю ссылку', async ({page, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const status = await request.get('/api/v1/auth/status').then(res => res.json());
  const login = await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'), {
    data:{username:'browserqa', password:'browser-qa-password'},
  });
  expect(login.ok()).toBeTruthy();
  const run = Date.now();
  const lib = await request.post('/api/v1/libraries', {data:{name:'Объединение ' + run, mode:'managed'}}).then(res => res.json());
  async function record(name: string) {
    const result = await request.post('/api/v1/libraries/' + lib.id + '/upload', {multipart:{file:{
      name:name + '.txt', mimeType:'text/plain', buffer:Buffer.from(name),
    }}});
    expect(result.ok()).toBeTruthy();
    const file = (await result.json()).file;
    return request.get('/api/v1/files/' + file.id + '/info').then(res => res.json());
  }
  const target = await record('Основное ' + run);
  const source = await record('Издание ' + run);
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/books/' + target.work_id);
  await page.getByRole('button', {name:'Объединить карточки'}).click();
  const merge = page.getByRole('region', {name:'Объединить карточки'});
  await merge.getByLabel('Найти вторую карточку').fill(source.title);
  await merge.getByRole('button', {name:'Найти', exact:true}).click();
  await merge.getByRole('button', {name:'Предпросмотр для ' + source.title}).click();
  await expect(merge.getByText('Из «' + source.title + '» в «' + target.title + '»')).toBeVisible();
  await merge.screenshot({path:testInfo.outputPath('desktop.png')});
  await page.setViewportSize({width:390, height:844});
  await merge.screenshot({path:testInfo.outputPath('mobile.png')});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await merge.getByRole('button', {name:'Подтвердить объединение'}).click();
  await expect(notifications(page).getByText('Карточки объединены.', {exact:true})).toBeVisible();
  await page.goto('/books/' + source.work_id);
  await expect(page).toHaveURL('/books/' + target.work_id);
  await expect(page.getByRole('heading', {name:target.title, exact:true})).toBeVisible();
  const canonical = await request.get('/api/v1/books/' + target.work_id).then(res => res.json());
  expect(canonical.editions.flatMap((edition: {files: {id: number}[]}) => edition.files.map(file => file.id)))
    .toEqual([target.id, source.id]);
});

test('настраивает сканирование библиотеки с телефона', async ({page, baseURL, request}) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const status = await request.get('/api/v1/auth/status').then(res => res.json());
  const login = await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'), {
    data: {username:'browserqa', password:'browser-qa-password'},
  });
  expect(login.ok()).toBeTruthy();
  const libraryName = 'Расписание ' + Date.now();
  const renamed = libraryName + ' (обновлено)';
  const created = await request.post('/api/v1/libraries', {data:{name:libraryName, mode:'managed'}});
  expect(created.ok()).toBeTruthy();
  const libraryId = (await created.json()).id;
  for (const cookie of (await request.storageState()).cookies) await page.context().addCookies([cookie]);
  await page.setViewportSize({width:390, height:844});
  await page.goto('/libraries');
  const library = page.locator('.library-panel').filter({has:page.getByRole('link',{name:libraryName,exact:true})});
  await library.getByRole('button',{name:'Настройки библиотеки',exact:true}).click();
  await library.getByLabel('Название',{exact:true}).fill(renamed);
  await library.getByRole('textbox',{name:'Описание',exact:true}).fill('Библиотека для проверки настроек');
  await library.getByRole('textbox',{name:'Форматы оригиналов через запятую',exact:true}).fill('EPUB, PDF');
  await library.getByLabel('Доступна другим пользователям').check();
  await library.getByRole('button',{name:'Сохранить настройки библиотеки',exact:true}).click();
  const editedLibrary = page.locator('.library-panel').filter({has:page.getByRole('link',{name:renamed,exact:true})});
  await expect(editedLibrary.getByText('Библиотека для проверки настроек',{exact:true})).toBeVisible();
  await expect(editedLibrary.getByText('Общий доступ')).toBeVisible();
  await editedLibrary.getByRole('button',{name:'Настройки библиотеки',exact:true}).click();
  await expect(editedLibrary.getByRole('textbox',{name:'Форматы оригиналов через запятую',exact:true})).toHaveValue('epub, pdf');
  expect((await request.patch('/api/v1/libraries/' + libraryId,{data:{shared:false}})).ok()).toBeTruthy();
  await editedLibrary.getByRole('textbox',{name:'Описание',exact:true}).fill('Описание изменено одновременно с доступом');
  await editedLibrary.getByRole('button',{name:'Сохранить настройки библиотеки',exact:true}).click();
  await expect(editedLibrary.getByText('Описание изменено одновременно с доступом',{exact:true})).toBeVisible();
  await expect(editedLibrary.getByText('Общий доступ')).toHaveCount(0);
  const disclosure = editedLibrary.getByText('Расписание сканирования', {exact:true});
  await disclosure.click();
  const interval = editedLibrary.getByLabel('Интервал в минутах');
  await editedLibrary.getByLabel('Режим').selectOption('preview');
  await interval.fill('120');
  await editedLibrary.getByLabel('Форматы через запятую').fill('epub, pdf');
  await editedLibrary.getByLabel('Сканировать при изменении файлов').check();
  await editedLibrary.getByRole('button', {name:'Сохранить расписание'}).click();
  const scheduleToast = notifications(page).getByText('Расписание сохранено.', {exact:true});
  await expect(scheduleToast).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.reload();
  await disclosure.click();
  await expect(editedLibrary.getByLabel('Режим')).toHaveValue('preview');
  await expect(interval).toHaveValue('120');
  await expect(editedLibrary.getByLabel('Сканировать при изменении файлов')).toBeChecked();
  await editedLibrary.getByLabel('Режим').selectOption('off');
  await page.clock.install();
  await editedLibrary.getByRole('button', {name:'Сохранить расписание'}).click();
  await expect(scheduleToast).toBeVisible();
  await page.mouse.move(0, 0);
  await page.clock.fastForward(9_000);
  await expect(scheduleToast).toHaveCount(0);
});

async function sample(chapterCount = 2) {
  const zip = new JSZip();
  const files = ['one', 'two', 'three'].slice(0, chapterCount);
  const manifest = files.map(filename => `<item id="${filename}" href="${filename}.xhtml" media-type="application/xhtml+xml"/>`).join('');
  const spine = files.map(filename => `<itemref idref="${filename}"/>`).join('');
  const contents = files.map((filename, index) => `<li><a href="${filename}.xhtml">Глава ${index + 1}</a></li>`).join('');
  zip.file('mimetype', 'application/epub+zip');
  zip.file('META-INF/container.xml', '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>');
  zip.file('book.opf', '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:qa:reader</dc:identifier><dc:title>Полярная книга</dc:title><dc:creator>Тестовый автор</dc:creator><dc:language>ru</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + manifest + '</manifest><spine>' + spine + '</spine></package>');
  zip.file('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Содержание</title></head><body><nav epub:type="toc"><ol>' + contents + '</ol></nav></body></html>');
  for (const [index, filename] of files.entries()) {
    const title = 'Глава ' + (index + 1);
    zip.file(filename + '.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>' + title +
      '</title></head><body><h1>' + title + '</h1><p>Текст для проверки электронного издания.</p></body></html>');
  }
  return zip.generateAsync({type: 'nodebuffer'});
}

test('сохраняет несинхронизированную позицию соседней вкладки и разрешает конфликт', async ({page, baseURL, request}) => {
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  const login = await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}});
  expect(login.ok()).toBeTruthy();
  const user = await login.json();
  const library = await request.post('/api/v1/libraries',{data:{name:'Конфликт ' + Date.now(),mode:'managed'}}).then(response => response.json());
  const upload = await request.post('/api/v1/libraries/' + library.id + '/upload', {multipart:{file:{name:'conflict.epub',
    mimeType:'application/epub+zip',buffer:await sample(3)}}});
  expect(upload.ok()).toBeTruthy();
  const id = (await upload.json()).file.id;
  const progressUrl = '/api/v1/reading/progress/' + id;
  const storageKey = 'polka:progress:' + user.id + ':' + id;
  const pendingChapter = (tab: Page) => tab.evaluate(key => JSON.parse(localStorage.getItem(key) || 'null')?.chapter, storageKey);
  const chapter = (tab: Page, number: number) => tab.frameLocator('.epub-canvas iframe').getByRole('heading',{name:'Глава ' + number});
  const state = (tab: Page) => tab.locator('.reader-footer').getByRole('status');
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/read/' + id);
  await expect(state(page)).toHaveText('Позиция сохранена');

  const other = await page.context().newPage();
  await other.goto('/read/' + id);
  await expect(chapter(other, 1)).toBeVisible();
  const firstSave = Promise.withResolvers<void>();
  const secondSave = Promise.withResolvers<void>();
  try {
    for (const [tab, gate] of [[page, firstSave], [other, secondSave]] as const) {
      await tab.route('**' + progressUrl, async route => {
        if (route.request().method() === 'PUT') await gate.promise;
        await route.continue();
      });
    }
    await page.getByLabel('Содержание').selectOption('two.xhtml');
    await expect.poll(() => pendingChapter(page)).toBe('Глава 2');
    await other.getByLabel('Содержание').selectOption('three.xhtml');
    await expect.poll(() => pendingChapter(other)).toBe('Глава 3');

    firstSave.resolve();
    await expect(state(page)).toHaveText('Позиция сохранена');
    expect(await pendingChapter(other)).toBe('Глава 3');
    secondSave.resolve();
    await other.getByRole('button',{name:'Перейти к позиции сервера',exact:true}).click();
    await expect(chapter(other, 2)).toBeVisible();
    const server = await request.get(progressUrl).then(response => response.json());
    expect(server.chapter).toBe('Глава 2');

    await page.getByLabel('Содержание').selectOption('one.xhtml');
    await expect.poll(async () => (await request.get(progressUrl).then(response => response.json())).chapter).toBe('Глава 1');
    await other.getByLabel('Содержание').selectOption('three.xhtml');
    await other.getByRole('button',{name:'Оставить мою',exact:true}).click();
    await expect(state(other)).toHaveText('Позиция сохранена');
    expect((await request.get(progressUrl).then(response => response.json())).chapter).toBe('Глава 3');
  } finally {
    firstSave.resolve();
    secondSave.resolve();
    await other.close();
  }
});

test('добавляет EPUB и продолжает чтение в другом браузере', async ({page, browser, baseURL, request}) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }

  await page.goto('/');
  const setup = await request.get('/api/v1/auth/status').then(res => res.json());
  await page.getByLabel('Имя пользователя').fill('browserqa');
  await page.getByLabel('Пароль').fill('browser-qa-password');
  await page.getByRole('button', {name: setup.setup ? 'Создать аккаунт' : 'Войти'}).click();
  await expect(page.getByRole('heading', {name:'Все книги'})).toBeVisible();
  const collectionName = 'Коллекция ' + Date.now();
  await page.getByRole('link', {name:'Коллекции', exact:true}).click();
  await page.getByLabel('Название коллекции').fill(collectionName);
  await page.getByRole('button', {name:'Создать коллекцию'}).click();
  await expect(page.getByRole('link', {name:collectionName})).toBeVisible();
  await page.getByRole('link', {name:'Все книги', exact:true}).click();

  await page.getByRole('link', {name:'Добавить книги'}).click();
  const libraryName = 'Тест ' + Date.now();
  await page.getByLabel('Название', {exact:true}).fill(libraryName);
  await page.getByRole('button', {name:'Создать библиотеку'}).click();
  const library = page.locator('.library-panel').filter({hasText: libraryName});
  await library.getByLabel('Файлы книг').setInputFiles({name:'reader.epub', mimeType:'application/epub+zip', buffer:await sample()});
  await library.getByRole('button', {name:'Загрузить файлы'}).click();
  await expect(notifications(page).getByText(/Добавлено 1/)).toBeVisible();
  await library.getByRole('link', {name:libraryName}).click();
  await page.getByRole('link', {name:/Полярная книга/}).click();
  const bookUrl = page.url();
  await expect(page.locator('.personal-controls time')).toHaveCount(0);
  await page.locator('.personal-controls summary').click();
  const collectionToggle = page.getByRole('checkbox', {name:collectionName});
  await collectionToggle.click();
  await expect(collectionToggle).toBeChecked();
  await page.getByLabel('Статус чтения').selectOption('planned');
  await page.getByRole('button', {name:'Избранное', exact:true}).click();
  await expect(page.getByRole('button', {name:'Избранное', exact:true})).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('link', {name:'Читать', exact:true}).click();

  const text = page.frameLocator('.epub-canvas iframe');
  await expect(text.getByRole('heading', {name:'Глава 1'})).toBeVisible();
  await page.getByLabel('Поиск по тексту книги').fill('Текст для проверки');
  await page.getByRole('button', {name:'Найти', exact:true}).click();
  await expect(page.getByText('Совпадений: 2')).toBeVisible();
  await text.getByText('Текст для проверки электронного издания.').evaluate(node => {
    const range = node.ownerDocument.createRange();
    range.selectNodeContents(node);
    const selected = node.ownerDocument.defaultView?.getSelection();
    selected?.removeAllRanges();
    selected?.addRange(range);
  });
  await page.getByLabel('Текст заметки').fill('Проверка выделения');
  await page.getByRole('button', {name:'Сохранить выделение'}).click();
  await expect(page.getByText('Проверка выделения', {exact:true})).toBeVisible();
  await page.getByLabel('Содержание').selectOption('two.xhtml');
  await expect(text.getByRole('heading', {name:'Глава 2'})).toBeVisible();
  await expect(page.locator('.reader-footer').getByRole('status')).toContainText('Позиция сохранена');
  await expect(page.locator('.reader-footer')).toContainText('Глава 2');

  const detail = await page.context().newPage();
  try {
    await detail.goto(bookUrl);
    const start = detail.getByText(/^Начало чтения/).locator('time');
    const recent = detail.getByText(/^Последнее чтение/).locator('time');
    await expect(start).toHaveAttribute('datetime', /T.+Z$|T.+[+-]\d{2}:\d{2}$/);
    await expect(recent).toBeVisible();
    await detail.getByLabel('Статус чтения').selectOption('finished');
    await expect(detail.getByText(/^Завершено/).locator('time')).toBeVisible();
    await expect(start).toBeVisible();
  } finally {
    await detail.close();
  }

  await page.reload();
  await expect(page.frameLocator('.epub-canvas iframe').getByRole('heading', {name:'Глава 2'})).toBeVisible();
  const alternate = await browser.newContext({viewport:{width:390, height:844}});
  try {
    const phone = await alternate.newPage();
    await phone.goto(page.url());
    await phone.getByLabel('Имя пользователя').fill('browserqa');
    await phone.getByLabel('Пароль').fill('browser-qa-password');
    await phone.getByRole('button', {name:'Войти'}).click();
    await expect(phone.frameLocator('.epub-canvas iframe').getByRole('heading', {name:'Глава 2'})).toBeVisible();
    await expect(phone.locator('.reader-footer')).toContainText('Глава 2');
    await phone.getByRole('button', {name:'Заметки', exact:true}).click();
    await expect(phone.getByText('Проверка выделения', {exact:true})).toBeVisible();
    expect(await phone.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await alternate.close();
  }
});

test('конвертирует FB2 фоновым обработчиком', async ({request, baseURL}) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const credentials = {username:'browserqa', password:'browser-qa-password'};
  const status = await request.get('/api/v1/auth/status').then(result => result.json());
  const login = await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'), {data:credentials});
  expect(login.ok()).toBeTruthy();
  const lib = await request.post('/api/v1/libraries', {data:{name:'Конвертация', mode:'managed'}}).then(result => result.json());
  const content = '<?xml version="1.0" encoding="utf-8"?>' +
    '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info><genre>sf</genre>' +
    '<author><first-name>Анна</first-name><last-name>Соколова</last-name></author>' +
    '<book-title>Тест конвертации</book-title><lang>ru</lang></title-info></description>' +
    '<body><section><title><p>Глава 1</p></title><p>Проверка реальной конвертации.</p></section></body></FictionBook>';
  const upload = await request.post('/api/v1/libraries/' + lib.id + '/upload', {
    multipart:{file:{name:'book.fb2', mimeType:'application/x-fictionbook+xml', buffer:Buffer.from(content)}},
  });
  expect(upload.status(), await upload.text()).toBe(201);
  await expect.poll(async () => {
    const result = await request.get('/api/v1/imports').then(response => response.json());
    const job = result.find((item: {kind: string; library_id: number}) => item.kind === 'conversion' && item.library_id === lib.id);
    return job?.status === 'error' ? job.error : job?.status;
  }, {timeout:30_000}).toBe('done');
  const books = await request.get('/api/v1/books?library_id=' + lib.id).then(result => result.json());
  const files = books.items[0].editions[0].files;
  expect(files.map((file: {format: string}) => file.format)).toEqual(['fb2', 'epub']);
  const source = await request.get('/api/v1/files/' + files[0].id);
  expect(await source.text()).toBe(content);
});

function samplePdf(count = 1, height = 200) {
  const stream = `BT /F1 20 Tf 32 ${height - 70} Td (A searchable line) Tj ET`;
  const page = `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 ${height}] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`;
  const extra = Array.from({length: count - 1}, (_, index) => (index + 6) + ' 0 R');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [' + ['3 0 R', ...extra].join(' ') + '] /Count ' + count + ' >>',
    page,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Length ' + stream.length + ' >>\nstream\n' + stream + '\nendstream',
    ...extra.map(() => page),
  ];
  let document = '%PDF-1.4\n';
  const offsets = objects.map((value, index) => {
    const at = document.length;
    document += (index + 1) + ' 0 obj\n' + value + '\nendobj\n';
    return at;
  });
  const xref = document.length;
  document += 'xref\n0 ' + (objects.length + 1) + '\n0000000000 65535 f \n' +
    offsets.map(offset => String(offset).padStart(10, '0') + ' 00000 n \n').join('') +
    'trailer\n<< /Size ' + (objects.length + 1) + ' /Root 1 0 R >>\nstartxref\n' + xref + '\n%%EOF\n';
  return Buffer.from(document, 'ascii');
}

test('предлагает продолжить чтение EPUB примерно на той же позиции в PDF', async ({page, request, baseURL}) => {
  const origin = new URL(baseURL!);
  if (!['localhost','127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') throw new Error('Требуется тестовый сервер на порту 18080');
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa',password:'browser-qa-password'}})).ok()).toBeTruthy();
  const library = await request.post('/api/v1/libraries',{data:{name:'Переход ' + Date.now(),mode:'managed'}}).then(response => response.json());
  const uploaded = await request.post('/api/v1/libraries/' + library.id + '/upload',{multipart:{file:{
    name:'reading.epub',mimeType:'application/epub+zip',buffer:await sample()}}});
  expect(uploaded.ok()).toBeTruthy();
  const epub = (await uploaded.json()).file.id;
  const info = await request.get('/api/v1/files/' + epub + '/info').then(response => response.json());
  const attachment = await request.post('/api/v1/books/' + info.work_id + '/editions/upload',{multipart:{file:{
    name:'reading.pdf',mimeType:'application/pdf',buffer:samplePdf(4)}}});
  expect(attachment.ok()).toBeTruthy();
  const pdf = (await attachment.json()).file.id;
  expect((await request.put('/api/v1/reading/progress/' + epub,{data:{
    version:0,locator:'epubcfi(/6/4!/4/2)',chapter:'Глава 2',percentage:0.55,device:'Планшет',
  }})).ok()).toBeTruthy();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/read/' + pdf);
  await expect(page.getByText('В другом файле этой книги (EPUB) прочитано 55%. Переход будет приблизительным.')).toBeVisible();
  await expect(page.getByLabel('Страница 1',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Оставить текущую',exact:true}).click();
  expect(await request.get('/api/v1/reading/progress/' + pdf).then(response => response.json())).toMatchObject({version:0});
  await page.getByRole('link',{name:'К книге',exact:true}).click();
  expect((await request.put('/api/v1/reading/progress/' + epub,{data:{
    version:1,locator:'epubcfi(/6/4!/4/2)',chapter:'Глава 2',percentage:0.63,device:'Планшет',
  }})).ok()).toBeTruthy();
  await page.goBack();
  await expect(page.getByText('В другом файле этой книги (EPUB) прочитано 63%. Переход будет приблизительным.')).toBeVisible();
  await page.getByRole('button',{name:'Перейти примерно',exact:true}).click();
  await expect(page.getByLabel('Страница 3',{exact:true})).toBeVisible();
  await expect(page.locator('.reader-footer').getByRole('status')).toContainText('Позиция сохранена');
  await page.reload();
  await expect(page.getByLabel('Страница 3',{exact:true})).toBeVisible();
  expect(await request.get('/api/v1/reading/progress/' + epub).then(response => response.json()))
    .toMatchObject({locator:'epubcfi(/6/4!/4/2)',chapter:'Глава 2',version:2});
});

test('находит документ KOReader по отпечатку PDF и ищет текст на странице', async ({page, request, baseURL}) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const credentials = {username:'browserqa', password:'browser-qa-password'};
  const status = await request.get('/api/v1/auth/status').then(result => result.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'), {data:credentials})).ok()).toBeTruthy();
  const lib = await request.post('/api/v1/libraries', {data:{name:'Проверка PDF', mode:'managed'}}).then(result => result.json());
  const pdf = Buffer.concat([samplePdf(), Buffer.from('% bind ' + Date.now() + '\n','ascii')]);
  const upload = await request.post('/api/v1/libraries/' + lib.id + '/upload', {
    multipart:{file:{name:'sample.pdf', mimeType:'application/pdf', buffer:pdf}},
  });
  expect(upload.status(), await upload.text()).toBe(201);
  const file = (await upload.json()).file;
  expect(pdf.length).toBeLessThan(1024);
  const document = createHash('md5').update(pdf).digest('hex');
  const native = await request.post('/api/v1/devices/koreader/credentials').then(response => response.json());
  expect((await request.put('/koreader/syncs/progress', {headers:{
    'x-auth-user':native.username, 'x-auth-key':createHash('md5').update(native.password).digest('hex'),
  },data:{document,progress:'1',percentage:0.4,device:'KOReader QA',device_id:'pdf-reader'}})).ok()).toBeTruthy();
  const info = await request.get('/api/v1/files/' + file.id + '/info').then(response => response.json());
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto('/books/' + info.work_id);
  await page.getByRole('button', {name:'Связать с KOReader',exact:true}).click();
  await page.getByRole('button', {name:'Найти по отпечатку файла',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'Идентификатор документа KOReader'})).toHaveValue(document);
  await page.getByRole('button',{name:'Связать',exact:true}).click();
  await expect(notifications(page).getByText('Документ KOReader связан с файлом')).toBeVisible();
  await page.getByRole('link',{name:'Читать',exact:true}).click();
  await expect(page.getByLabel('Страница 1', {exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Оставить текущую',exact:true}).click();
  await page.getByLabel('Поиск по тексту книги').fill('searchable');
  await page.getByRole('button', {name:'Найти', exact:true}).click();
  await expect(page.getByText('Найдено на странице 1')).toBeVisible();
});

test('читает увеличенный PDF в сенсорном профиле планшета', async ({browser, request, baseURL}, testInfo) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const status = await request.get('/api/v1/auth/status').then(response => response.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'),
    {data:{username:'browserqa', password:'browser-qa-password'}})).ok()).toBeTruthy();
  const library = await request.post('/api/v1/libraries', {data:{name:'Планшет PDF ' + Date.now(), mode:'managed'}})
    .then(response => response.json());
  const upload = await request.post('/api/v1/libraries/' + library.id + '/upload', {
    multipart:{file:{name:'tablet.pdf', mimeType:'application/pdf', buffer:samplePdf(2, 500)}},
  });
  expect(upload.status(), await upload.text()).toBe(201);
  const file = (await upload.json()).file;
  const tablet = await browser.newContext({storageState:await request.storageState(),
    viewport:{width:820,height:1180}, deviceScaleFactor:2, isMobile:true, hasTouch:true});
  try {
    const page = await tablet.newPage();
    await page.goto('/read/' + file.id);
    const pane = page.locator('.pdf-canvas');
    const canvas = pane.locator('canvas');
    const zoom = page.getByRole('button', {name:'Увеличить масштаб'});
    const next = page.getByRole('button', {name:'Следующая страница'});
    await expect(next).toBeEnabled();
    await expect.poll(() => canvas.evaluate(el => el.width / el.getBoundingClientRect().width)).toBeGreaterThan(1.9);
    expect((await zoom.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    if (process.env.EBOOK_CAPTURE) await page.screenshot({path:testInfo.outputPath('pdf-tablet-portrait.png'), scale:'css'});
    for (let index = 0; index < 4; index += 1) await zoom.tap();
    await expect.poll(() => pane.evaluate(el => el.scrollWidth - el.clientWidth)).toBeGreaterThan(200);
    expect(await canvas.evaluate(el => el.getBoundingClientRect().left >= el.parentElement!.getBoundingClientRect().left)).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(820);
    const cdp = await tablet.newCDPSession(page);
    const box = (await pane.boundingBox())!;
    const y = Math.round(box.y + 100);
    const x = Math.round(box.x + 0.75 * box.width);
    await pane.evaluate(el => {
      el.addEventListener('scrollend', () => el.setAttribute('data-test-scroll-ended', ''), {once:true});
    });
    await cdp.send('Input.dispatchTouchEvent', {type:'touchStart', touchPoints:[{x,y}]});
    for (const distance of [35, 75, 130]) {
      await cdp.send('Input.dispatchTouchEvent', {type:'touchMove', touchPoints:[{x:x-distance,y}]});
    }
    await cdp.send('Input.dispatchTouchEvent', {type:'touchEnd', touchPoints:[]});
    await expect.poll(() => pane.evaluate(el => el.scrollLeft)).toBeGreaterThan(10);
    await expect(pane).toHaveAttribute('data-test-scroll-ended', '');
    await pane.evaluate(el => {el.scrollTop = 140;});
    await expect.poll(() => pane.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
    await next.tap();
    await expect(canvas).toHaveAccessibleName('Страница 2');
    await expect.poll(() => pane.evaluate(el => [el.scrollLeft, el.scrollTop])).toEqual([0,0]);
    await expect(page.locator('.reader-footer')).toContainText('Страница 2');
    await page.setViewportSize({width:1180,height:820});
    await page.reload();
    await expect(canvas).toHaveAccessibleName('Страница 2');
    await expect.poll(() => request.get('/api/v1/reading/progress/' + file.id)
      .then(response => response.json()).then(position => position.locator)).toBe('2');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1180);
    if (process.env.EBOOK_CAPTURE) await page.screenshot({path:testInfo.outputPath('pdf-tablet-landscape.png'), scale:'css'});
  } finally {
    await tablet.close();
  }
});

test('подтверждает раскладку и восстанавливает книгу из корзины', async ({page, request, baseURL}) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const credentials = {username:'browserqa', password:'browser-qa-password'};
  const status = await request.get('/api/v1/auth/status').then(result => result.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'), {data:credentials})).ok()).toBeTruthy();
  const library = await request.post('/api/v1/libraries', {data:{name:'Раскладка ' + Date.now(), mode:'managed'}}).then(result => result.json());
  const upload = await request.post('/api/v1/libraries/' + library.id + '/upload', {
    multipart:{file:{name:'Русская книга.txt', mimeType:'text/plain', buffer:Buffer.from('Тестовое издание')}},
  });
  expect(upload.status(), await upload.text()).toBe(201);
  const file = (await upload.json()).file;
  const entry = await request.get('/api/v1/files/' + file.id + '/info').then(response => response.json());
  await page.goto('/libraries/' + library.id + '/layout');
  await page.getByLabel('Имя пользователя').fill(credentials.username);
  await page.getByLabel('Пароль').fill(credentials.password);
  await page.getByRole('button', {name:'Войти'}).click();
  await page.getByLabel('Шаблон пути').fill('{title}.{format}');
  await page.getByRole('button', {name:'Сохранить шаблон'}).click();
  await page.getByRole('button', {name:'Построить план по сохранённому шаблону'}).click();
  await expect(page.getByText('Русская книга.txt', {exact:true})).toBeVisible();
  await page.getByRole('button', {name:'Подтвердить раскладку'}).click();
  await expect(page.getByRole('heading', {name:'Активная раскладка'})).toBeVisible({timeout:15_000});
  await expect(page.getByRole('link', {name:'Скачать манифест'})).toBeVisible();
  const manifest = await request.get('/api/v1/libraries/' + library.id + '/layout/manifest').then(response => response.json());
  expect(manifest.files.map((row: {path:string}) => row.path)).toEqual(['Русская книга.txt']);
  if (process.env.EBOOK_CAPTURE) await page.screenshot({path:'local-data/qa/layout-desktop.png', fullPage:true});
  await page.setViewportSize({width:390, height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  if (process.env.EBOOK_CAPTURE) await page.screenshot({path:'local-data/qa/layout-mobile.png', fullPage:true});
  const removeSnapshot = page.getByRole('button', {name:'Удалить снимок'});
  await expect(removeSnapshot).toHaveCount(0);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', {name:'Отменить последнюю раскладку'}).click();
  await expect(removeSnapshot).toBeVisible();
  page.once('dialog', dialog => dialog.dismiss());
  await removeSnapshot.click();
  await expect(removeSnapshot).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await removeSnapshot.click();
  await expect(removeSnapshot).toHaveCount(0);
  expect(await request.get('/api/v1/files/' + file.id).then(response => response.text())).toBe('Тестовое издание');
  await page.goto('/books/' + entry.work_id);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', {name:'В корзину'}).click();
  await expect(page.getByRole('heading', {name:'Корзина'})).toBeVisible();
  expect((await request.get('/api/v1/files/' + file.id)).status()).toBe(404);
  const row = page.locator('.rows li').filter({hasText:'Русская книга'});
  await row.getByRole('button', {name:'Восстановить'}).click();
  await expect(row).toHaveCount(0);
  expect(await request.get('/api/v1/files/' + file.id).then(response => response.text())).toBe('Тестовое издание');
  await page.goto('/books/' + entry.work_id);
  await page.getByText('Добавить издание или формат', {exact:true}).click();
  await page.getByLabel('Файл издания').setInputFiles({name:'Другое издание.txt', mimeType:'text/plain',
    buffer:Buffer.from('Достаточный размер для проверки загрузки.\n'.repeat(9000))});
  await page.getByRole('button', {name:'Прикрепить файл'}).click();
  await expect(page.getByText(/Другое издание\.txt/)).toBeVisible();
  const updated = await request.get('/api/v1/books/' + entry.work_id).then(response => response.json());
  expect(updated.editions).toHaveLength(2);
});

test('создаёт пакет Sony EPUB 2 и принимает ручную позицию', async ({page, request, baseURL}) => {
  const origin = new URL(baseURL!);
  if (!['localhost', '127.0.0.1'].includes(origin.hostname) || origin.port !== '18080') {
    throw new Error('E2E требует отдельный экземпляр на локальном порту 18080.');
  }
  const credentials = {username:'browserqa', password:'browser-qa-password'};
  const status = await request.get('/api/v1/auth/status').then(result => result.json());
  expect((await request.post('/api/v1/auth/' + (status.setup ? 'setup' : 'login'), {data:credentials})).ok()).toBeTruthy();
  const library = await request.post('/api/v1/libraries', {data:{name:'Sony ' + Date.now(), mode:'managed'}}).then(response => response.json());
  const uploaded = await request.post('/api/v1/libraries/' + library.id + '/upload', {
    multipart:{file:{name:'sony.epub', mimeType:'application/epub+zip', buffer:await sample()}},
  });
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const file = (await uploaded.json()).file;
  const pdfSource = samplePdf();
  const pdfName = 'sony-scan-' + library.id + '.pdf';
  const pdfUpload = await request.post('/api/v1/libraries/' + library.id + '/upload', {
    multipart:{file:{name:pdfName, mimeType:'application/pdf', buffer:pdfSource}},
  });
  expect(pdfUpload.status(), await pdfUpload.text()).toBe(201);
  const base = '/api/v1/devices/sony';
  const known = await request.get(base + '/exports').then(response => response.json());
  const lowerId = known[0]?.id || 0;
  await page.goto('/devices/sony?file=' + file.id);
  await page.getByLabel('Имя пользователя').fill(credentials.username);
  await page.getByLabel('Пароль').fill(credentials.password);
  await page.getByRole('button', {name:'Войти'}).click();
  const query = page.getByLabel('Поиск книг для Sony');
  await query.fill(pdfName.slice(0, -4));
  await page.getByRole('button', {name:'Найти', exact:true}).click();
  await page.getByRole('checkbox', {name:'PDF · ' + pdfName}).check();
  await page.getByRole('button', {name:'Посмотреть план экспорта'}).click();
  await expect(page.getByText('EPUB → EPUB 2', {exact:true})).toBeVisible();
  await expect(page.getByText('PDF → PDF', {exact:true})).toBeVisible();
  await page.getByRole('button', {name:'Подготовить ZIP'}).click();
  await expect.poll(async () => {
    const latest = await request.get(base + '/exports').then(response => response.json());
    return latest[0]?.id > lowerId ? latest[0].status === 'error' ? latest[0].error : latest[0].status : null;
  }, {timeout:45_000}).toBe('done');
  const jobs = await request.get(base + '/exports').then(response => response.json());
  const download = await request.get(base + '/exports/' + jobs[0].id + '/archive');
  expect(download.status()).toBe(200);
  const zip = await JSZip.loadAsync(await download.body());
  const manifest = JSON.parse(await zip.file('manifest.json')!.async('string'));
  const entry = manifest.files.find((file: {format:string}) => file.format === 'epub');
  const pdfEntry = manifest.files.find((file: {format:string}) => file.format === 'pdf');
  const epub = await zip.file(entry.filename)!.async('nodebuffer');
  expect(entry.format).toBe('epub');
  expect(createHash('sha256').update(epub).digest('hex')).toBe(entry.sha256);
  const edition = await JSZip.loadAsync(epub);
  const opf = await edition.file(/\.opf$/)[0].async('string');
  expect(opf).toMatch(/<package\b[^>]*\bversion="2\.0"/);
  expect(await zip.file(pdfEntry.filename)!.async('nodebuffer')).toEqual(pdfSource);
  await expect(page.locator('a[href="' + base + '/exports/' + jobs[0].id + '/archive"]')).toBeVisible();
  if (process.env.EBOOK_CAPTURE) await page.screenshot({path:'local-data/qa/sony-desktop.png', fullPage:true});
  await page.setViewportSize({width:390, height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  if (process.env.EBOOK_CAPTURE) await page.screenshot({path:'local-data/qa/sony-mobile.png', fullPage:true});
  await page.getByLabel('Прочитано, %').fill('42');
  await page.getByLabel('Страница на устройстве').fill('17');
  await page.getByRole('button', {name:'Сохранить позицию'}).click();
  await expect(notifications(page).getByText('Позиция Sony сохранена', {exact:true})).toBeVisible();
  await page.goto('/read/' + file.id);
  await expect(page.getByText(/Последняя отметка Sony PRS-600.*42%/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/Последняя отметка Sony PRS-600.*42%/)).toBeVisible();
});


import { readFileSync } from 'fs';

// Load .env from working directory (no external dotenv dependency needed)
try {
  const lines = readFileSync('.env', 'utf8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).trim();
    if (!(key in process.env)) process.env[key] = val;
  }
} catch { /* no .env file, rely on env vars being pre-set */ }

const NOTION_TOKEN = process.env.NOTION_TOKEN;
const NOTION_DATABASE_ID = process.env.NOTION_DATABASE_ID;
const DISCORD_WEBHOOK_URL = process.env.DISCORD_WEBHOOK_URL;

interface Template {
  slug: string;
  title: string;
  url: string;
}

// ---------------------------------------------------------------------------
// Fetching & parsing
// ---------------------------------------------------------------------------

async function fetchFramerTemplates(): Promise<Template[]> {
  console.log('Fetching Framer marketplace...');

  const res = await fetch('https://www.framer.com/marketplace/templates/?sort=recent', {
    headers: { 'User-Agent': 'Mozilla/5.0 (compatible; automation-bot/1.0)' },
  });
  if (!res.ok) throw new Error(`Framer fetch failed: ${res.status}`);

  const html = await res.text();
  const templates = parseNextJsStreamingData(html);

  if (templates.length > 0) {
    console.log(`Parsed ${templates.length} templates from Next.js streaming data.`);
    return templates;
  }

  console.log('No templates found in HTML — falling back to defuddle...');
  return fetchFromDefuddle();
}

function parseNextJsStreamingData(html: string): Template[] {
  const seen = new Set<string>();
  const templates: Template[] = [];

  // Next.js streaming embeds JSON payloads in self.__next_f.push([...]) calls.
  // Template objects appear with adjacent "slug" and "title" fields.
  // We scan the whole HTML for this pattern to avoid brittle structural parsing.
  const regex = /"slug"\s*:\s*"([a-z0-9][a-z0-9-]*)"[^}]{0,200}?"title"\s*:\s*"([^"]+)"/g;

  for (const [, slug, title] of html.matchAll(regex)) {
    // Filter out non-template slugs (e.g. creator profile slugs, nav items)
    if (seen.has(slug) || slug.length < 3) continue;
    seen.add(slug);
    templates.push({
      slug,
      title,
      url: `https://www.framer.com/marketplace/templates/${slug}/`,
    });
  }

  return templates;
}

async function fetchFromDefuddle(): Promise<Template[]> {
  const res = await fetch(
    'https://defuddle.md/www.framer.com/marketplace/templates/?sort=recent',
  );
  if (!res.ok) throw new Error(`Defuddle fetch failed: ${res.status}`);

  const html = await res.text();
  const seen = new Set<string>();
  const templates: Template[] = [];

  // Defuddle returns cleaned HTML — links are relative Framer paths
  const linkRegex = /href="\/marketplace\/templates\/([a-z0-9][a-z0-9-]+)\/"/g;

  for (const [, slug] of html.matchAll(linkRegex)) {
    if (seen.has(slug)) continue;
    seen.add(slug);

    // Try to find a nearby title; fall back to capitalising the slug
    const titleGuess = slug
      .split('-')
      .map(w => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');

    templates.push({
      slug,
      title: titleGuess,
      url: `https://www.framer.com/marketplace/templates/${slug}/`,
    });
  }

  console.log(`Parsed ${templates.length} templates from defuddle.`);
  return templates;
}

// ---------------------------------------------------------------------------
// Notion state store
// ---------------------------------------------------------------------------

async function getSeenSlugs(): Promise<Set<string>> {
  const slugs = new Set<string>();
  let cursor: string | undefined;

  do {
    const body: Record<string, unknown> = { page_size: 100 };
    if (cursor) body.start_cursor = cursor;

    const res = await fetch(`https://api.notion.com/v1/databases/${NOTION_DATABASE_ID}/query`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${NOTION_TOKEN}`,
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    const data = (await res.json()) as {
      results: Array<{ properties: { Slug?: { rich_text: Array<{ plain_text: string }> } } }>;
      has_more: boolean;
      next_cursor: string | null;
    };
    if (!res.ok) throw new Error(`Notion query failed: ${JSON.stringify(data)}`);

    for (const page of data.results) {
      const slug = page.properties.Slug?.rich_text?.[0]?.plain_text;
      if (slug) slugs.add(slug);
    }

    cursor = data.has_more && data.next_cursor ? data.next_cursor : undefined;
  } while (cursor);

  return slugs;
}

async function saveToNotion(template: Template): Promise<void> {
  const res = await fetch('https://api.notion.com/v1/pages', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${NOTION_TOKEN}`,
      'Notion-Version': '2022-06-28',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      parent: { database_id: NOTION_DATABASE_ID },
      properties: {
        Name: { title: [{ text: { content: template.title } }] },
        Slug: { rich_text: [{ text: { content: template.slug } }] },
        URL: { url: template.url },
        Discovered: { date: { start: new Date().toISOString().split('T')[0] } },
      },
    }),
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`Notion save failed for "${template.slug}": ${JSON.stringify(err)}`);
  }
}

// ---------------------------------------------------------------------------
// Discord notification
// ---------------------------------------------------------------------------

async function notifyDiscord(template: Template): Promise<void> {
  const res = await fetch(DISCORD_WEBHOOK_URL!, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      content: `New Framer template: **${template.title}** — ${template.url}`,
    }),
  });
  if (!res.ok) {
    console.warn(`Discord notification failed for "${template.title}": ${res.status}`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  if (!NOTION_TOKEN || !NOTION_DATABASE_ID || !DISCORD_WEBHOOK_URL) {
    console.error(
      'Missing required env vars: NOTION_TOKEN, NOTION_DATABASE_ID, DISCORD_WEBHOOK_URL',
    );
    process.exit(1);
  }

  const [templates, seenSlugs] = await Promise.all([fetchFramerTemplates(), getSeenSlugs()]);

  const newTemplates = templates.filter(t => !seenSlugs.has(t.slug));
  const isFirstRun = seenSlugs.size === 0;

  console.log(`${templates.length} templates fetched, ${newTemplates.length} new.`);

  if (newTemplates.length === 0) {
    console.log('Nothing new. All done.');
    return;
  }

  if (isFirstRun) {
    console.log('First run — seeding DB without Discord notifications to avoid spam.');
  }

  for (const template of newTemplates) {
    if (!isFirstRun) {
      await notifyDiscord(template);
    }
    await saveToNotion(template);
    console.log(`${isFirstRun ? 'Seeded' : 'Notified + saved'}: ${template.title}`);
  }

  console.log(
    `Done. ${isFirstRun ? 'Seeded' : 'Notified'} ${newTemplates.length} template(s).`,
  );
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});

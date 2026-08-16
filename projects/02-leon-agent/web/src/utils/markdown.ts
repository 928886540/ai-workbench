function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function safeHref(value: unknown): string {
  try {
    const base = typeof location === "undefined" ? "http://localhost" : location.origin;
    const raw = String(value ?? "").trim();
    if (!raw) return "";
    const url = new URL(raw, base);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

export function isImageHref(value: string): boolean {
  try {
    const base = typeof location === "undefined" ? "http://localhost" : location.origin;
    const url = new URL(value, base);
    const candidates = [
      url.pathname,
      url.searchParams.get("filename") || "",
      url.searchParams.get("file") || "",
    ];
    return candidates.some((candidate) =>
      /\.(?:avif|bmp|gif|jpe?g|png|webp)$/i.test(decodeURIComponent(candidate)),
    );
  } catch {
    return false;
  }
}

function markdownLinkPattern(): RegExp {
  return /!?\[[^\]]*\]\(([^)\s]+)\)/g;
}

function bareUrlPattern(): RegExp {
  return /https?:\/\/[^\s<>"']+/gi;
}

function cleanBareUrl(value: string): string {
  return value.replace(/[.,;!?，。；！？）)\]}]+$/, "");
}

export function extractImageHrefs(raw: string): string[] {
  const text = String(raw || "");
  const urls: string[] = [];
  const append = (value: string): void => {
    const href = safeHref(cleanBareUrl(value));
    if (href && isImageHref(href) && !urls.includes(href)) urls.push(href);
  };
  for (const match of text.matchAll(markdownLinkPattern())) append(match[1]);
  // Markdown links already contributed their href. Scan only the remaining
  // prose so the absolute URL inside `![...](https://...)` is not extracted a
  // second time, often with the closing Markdown parenthesis attached.
  const prose = text.replace(markdownLinkPattern(), "");
  for (const match of prose.matchAll(bareUrlPattern())) append(match[0]);
  return urls;
}

function isImageLabel(value: string): boolean {
  const normalized = value.replace(/[\s*_`#]/g, "").trim();
  return /^(?:(?:最新)?第\d+张(?:图片|图)?|图片\d+)[:：]?$/.test(normalized);
}

/** Remove image links from prose after their URLs have been promoted to image cards. */
export function stripImageLinks(raw: string): string {
  const lines = String(raw || "").split(/\r?\n/);
  const visible: string[] = [];
  for (const line of lines) {
    const hasImageLink = extractImageHrefs(line).length > 0;
    const withoutMarkdown = line.replace(markdownLinkPattern(), (whole, url: string) =>
      isImageHref(safeHref(url)) ? "" : whole,
    );
    const withoutBareUrls = withoutMarkdown.replace(bareUrlPattern(), (whole) =>
      isImageHref(safeHref(cleanBareUrl(whole))) ? "" : whole,
    );
    const cleaned = withoutBareUrls.trim();
    if (hasImageLink && isImageLabel(cleaned)) continue;
    if (/^\s*(?:\d+[.)]|[-*+])?\s*$/.test(withoutBareUrls)) {
      if (hasImageLink && visible.length && isImageLabel(visible[visible.length - 1])) {
        visible.pop();
      }
      continue;
    }
    if (cleaned) visible.push(withoutBareUrls);
  }
  return visible.join("\n");
}

function renderInline(raw: string): string {
  const tokens: string[] = [];
  const token = (html: string): string => {
    tokens.push(html);
    return `\u0000${tokens.length - 1}\u0000`;
  };
  let text = raw
    .replace(/`([^`]+)`/g, (_, code: string) => token(`<code>${escapeHtml(code)}</code>`))
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, label: string, url: string) => {
      const href = safeHref(url);
      return href
        ? token(
            `<a class="markdown-image-link" href="${escapeHtml(href)}"><img class="markdown-image" src="${escapeHtml(href)}" alt="${escapeHtml(label || "图片")}" loading="lazy"></a>`,
          )
        : label || "图片";
    })
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label: string, url: string) => {
      const href = safeHref(url);
      return href
        ? token(
            `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`,
          )
        : label;
    })
    .replace(/https?:\/\/[^\s<>"']+/gi, (rawUrl: string) => {
      const value = cleanBareUrl(rawUrl);
      const suffix = rawUrl.slice(value.length);
      const href = safeHref(value);
      if (!href || !isImageHref(href)) return rawUrl;
      return `${token(
        `<a class="markdown-image-link" href="${escapeHtml(href)}"><img class="markdown-image" src="${escapeHtml(href)}" alt="生成图片" loading="lazy"></a>`,
      )}${suffix}`;
    });
  text = escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return text.replace(/\u0000(\d+)\u0000/g, (_, index: string) => tokens[Number(index)] || "");
}

export function renderMarkdown(raw: string): string {
  const lines = String(raw || "").split(/\r?\n/);
  const html: string[] = [];
  let inCode = false;
  let code: string[] = [];
  let inList = false;
  const closeList = (): void => {
    if (!inList) return;
    html.push("</ul>");
    inList = false;
  };
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = [];
        inCode = false;
      } else {
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(line);
      continue;
    }
    const item = line.match(/^\s*[-*]\s+(.+)$/);
    if (item) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInline(item[1])}</li>`);
      continue;
    }
    closeList();
    if (!line.trim()) continue;
    const heading = line.match(/^#{1,3}\s+(.+)$/);
    if (heading) {
      html.push(`<h3>${renderInline(heading[1])}</h3>`);
      continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      html.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
      continue;
    }
    html.push(`<p>${renderInline(line)}</p>`);
  }
  closeList();
  if (inCode) html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  return html.join("");
}

export function renderExplicitImages(urls: string[], text: string): string {
  const seen = new Set<string>();
  const html: string[] = [];
  for (const rawUrl of urls) {
    const href = safeHref(rawUrl);
    if (!href || seen.has(href) || text.includes(rawUrl)) continue;
    seen.add(href);
    html.push(
      `<a class="markdown-image-link" href="${escapeHtml(href)}"><img class="markdown-image" src="${escapeHtml(href)}" alt="生成图片" loading="lazy"></a>`,
    );
  }
  return html.join("");
}

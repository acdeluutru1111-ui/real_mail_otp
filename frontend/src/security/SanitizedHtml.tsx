// Renders server-sanitized HTML message bodies inside a hardened sandboxed
// iframe. We use srcdoc with sandbox="" which means:
//   - NO allow-scripts  -> the embedded HTML cannot execute JavaScript.
//   - NO allow-same-origin -> the frame gets an opaque origin and cannot reach
//     the parent DOM, cookies, storage, or our tokens.
// This is the ONLY place message HTML is rendered. We never use
// dangerouslySetInnerHTML into the app DOM.

import { useMemo } from 'react';

export interface SanitizedHtmlProps {
  html: string;
  title?: string;
  className?: string;
}

// Wrap the sanitized body in a minimal document with its own restrictive CSP,
// so that even if the server sanitization missed something, the frame cannot
// load remote scripts or make network calls. Styles/images are permitted for
// readable rendering; scripts are blocked at both the sandbox and CSP layers.
function buildSrcDoc(bodyHtml: string): string {
  return [
    '<!doctype html>',
    '<html>',
    '<head>',
    '<meta charset="utf-8">',
    '<meta http-equiv="Content-Security-Policy" ' +
      "content=\"default-src 'none'; img-src data: https:; style-src 'unsafe-inline'; font-src data: https:\">",
    '<style>',
    'html,body{margin:0;padding:12px;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111;word-break:break-word;}',
    'img{max-width:100%;height:auto;}',
    'a{pointer-events:none;color:#0b5fff;}',
    '</style>',
    '</head>',
    '<body>',
    bodyHtml,
    '</body>',
    '</html>',
  ].join('');
}

export function SanitizedHtml({ html, title, className }: SanitizedHtmlProps) {
  const srcDoc = useMemo(() => buildSrcDoc(html ?? ''), [html]);

  return (
    <iframe
      // sandbox="" => most restrictive: no scripts, no same-origin, no forms,
      // no popups, no top navigation. Do NOT add allow-scripts/allow-same-origin.
      sandbox=""
      srcDoc={srcDoc}
      title={title ?? 'Message body'}
      className={className ?? 'sanitized-html-frame'}
      referrerPolicy="no-referrer"
    />
  );
}

export default SanitizedHtml;

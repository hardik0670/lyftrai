import React, { useEffect, useRef } from "react";
import DOMPurify from "dompurify";

function SearchSuggestions({ html }) {
  const host = useRef(null);

  useEffect(() => {
    if (!html) return;
    const fragment = DOMPurify.sanitize(`<div>${html}</div>`, {
      ALLOWED_TAGS: ["style", "div", "span", "a", "svg", "circle", "path"],
      ALLOWED_ATTR: ["class", "href", "width", "height", "viewBox", "xmlns", "cx", "cy", "r", "d", "fill", "fill-rule", "clip-rule"],
      ALLOWED_URI_REGEXP: /^https:\/\//i,
      FORCE_BODY: true,
      RETURN_DOM_FRAGMENT: true,
    });
    // Keep Google's widget styles local to the widget.
    for (const style of fragment.querySelectorAll("style")) {
      if (/@import|url\s*\(/i.test(style.textContent)) style.remove();
    }
    for (const link of fragment.querySelectorAll("a[href]")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    const shadow = host.current.shadowRoot ?? host.current.attachShadow({ mode: "open" });
    shadow.replaceChildren(fragment);
  }, [html]);

  return html ? <div className="search-suggestions" ref={host} /> : null;
}

export default function SearchSources({ result }) {
  return (
    <section className="search-sources" aria-label="Search sources">
      <h3>Sources</h3>
      <ul>
        {result.sources.map(({ title, url }) => (
          <li key={url}>
            <a href={url} target="_blank" rel="noopener noreferrer">{title}</a>
          </li>
        ))}
      </ul>
      <SearchSuggestions html={result.suggestions_html} />
    </section>
  );
}

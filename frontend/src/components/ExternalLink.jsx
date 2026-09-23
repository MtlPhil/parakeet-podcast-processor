// Links only http(s) URLs: feed-supplied URLs are untrusted and could carry
// a javascript: scheme. Anything else is shown as plain text.
export default function ExternalLink({ href, className }) {
  if (!/^https?:\/\//i.test(href || '')) {
    return <span className={className}>{href}</span>;
  }
  return (
    <a href={href} target="_blank" rel="noreferrer" className={`${className} hover:underline`}>
      {href}
    </a>
  );
}

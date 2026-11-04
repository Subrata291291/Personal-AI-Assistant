import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'

function normalizeLineBreakTags(markdown) {
  return markdown.replace(/<br\s*\/?>/gi, '  \n')
}

function safeMarkdownUrl(value) {
  const candidate = value.trim()
  if (!candidate) return ''

  try {
    const url = new URL(candidate)
    if (!['http:', 'https:', 'mailto:'].includes(url.protocol)) {
      return ''
    }
    if (url.username || url.password) return ''
    return url.href
  } catch {
    return ''
  }
}

const markdownComponents = {
  a: ({ href, children, ...props }) => (
    href ? (
      <a
        {...props}
        href={href}
        target="_blank"
        rel="noopener noreferrer nofollow"
        className="break-all text-violet-500 underline decoration-violet-400/50 underline-offset-2 hover:text-violet-400"
      >
        {children}
      </a>
    ) : (
      <>{children}</>
    )
  ),
  blockquote: ({ children, ...props }) => (
    <blockquote
      {...props}
      className="my-3 border-l-4 border-violet-400/60 pl-4 italic opacity-90"
    >
      {children}
    </blockquote>
  ),
  code: ({ className, children, ...props }) => {
    const isBlock = Boolean(className?.startsWith('language-'))
    return (
      <code
        {...props}
        className={`${isBlock
          ? 'block min-w-0 whitespace-pre p-3'
          : 'rounded bg-black/10 px-1.5 py-0.5 dark:bg-white/10'
        } font-mono text-[0.9em] ${className || ''}`}
      >
        {children}
      </code>
    )
  },
  h1: ({ children, ...props }) => (
    <h1 {...props} className="mb-3 mt-6 text-2xl font-bold leading-tight first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children, ...props }) => (
    <h2 {...props} className="mb-2 mt-5 text-xl font-bold leading-tight first:mt-0">
      {children}
    </h2>
  ),
  h3: ({ children, ...props }) => (
    <h3 {...props} className="mb-2 mt-4 text-lg font-semibold leading-tight first:mt-0">
      {children}
    </h3>
  ),
  h4: ({ children, ...props }) => (
    <h4 {...props} className="mb-2 mt-4 text-base font-semibold leading-tight first:mt-0">
      {children}
    </h4>
  ),
  h5: ({ children, ...props }) => (
    <h5 {...props} className="mb-2 mt-3 text-sm font-semibold leading-tight first:mt-0">
      {children}
    </h5>
  ),
  h6: ({ children, ...props }) => (
    <h6 {...props} className="mb-2 mt-3 text-xs font-semibold uppercase leading-tight first:mt-0">
      {children}
    </h6>
  ),
  hr: (props) => <hr {...props} className="my-5 border-current opacity-20" />,
  ol: ({ children, ...props }) => (
    <ol {...props} className="my-3 list-decimal space-y-1 pl-6">
      {children}
    </ol>
  ),
  p: ({ children, ...props }) => (
    <p {...props} className="my-3 whitespace-normal first:mt-0 last:mb-0">
      {children}
    </p>
  ),
  pre: ({ children, ...props }) => (
    <pre
      {...props}
      className="my-3 max-w-full overflow-x-auto rounded-lg bg-black/10 text-sm dark:bg-black/30"
    >
      {children}
    </pre>
  ),
  table: ({ children, ...props }) => (
    <div className="my-4 max-w-full overflow-x-auto rounded-lg border border-current/15">
      <table {...props} className="w-full border-collapse text-left text-sm">
        {children}
      </table>
    </div>
  ),
  th: ({ children, ...props }) => (
    <th {...props} className="whitespace-nowrap border-b border-current/20 px-3 py-2 font-semibold">
      {children}
    </th>
  ),
  td: ({ children, ...props }) => (
    <td {...props} className="max-w-[24rem] break-words border-b border-current/10 px-3 py-2 align-top">
      {children}
    </td>
  ),
  ul: ({ children, ...props }) => (
    <ul {...props} className="my-3 list-disc space-y-1 pl-6">
      {children}
    </ul>
  ),
}

export default function AssistantMarkdown({ content }) {
  return (
    <div className="assistant-markdown min-w-0 max-w-full break-words [overflow-wrap:anywhere]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        urlTransform={safeMarkdownUrl}
        components={markdownComponents}
      >
        {normalizeLineBreakTags(content)}
      </ReactMarkdown>
    </div>
  )
}

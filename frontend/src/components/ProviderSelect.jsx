// Per-request LLM provider picker for on-demand generation (synopsis,
// LinkedIn and blog posts). An empty value uses the provider configured in
// Settings; Gemini can be chosen explicitly for a single request.
export default function ProviderSelect({ value, onChange, className = '' }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`border rounded-lg px-2 py-1 text-xs text-gray-600 ${className}`}
    >
      <option value="">Default provider</option>
      <option value="gemini">Gemini</option>
    </select>
  );
}

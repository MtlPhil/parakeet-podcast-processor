const LANGUAGES = [
  ['en', 'English'],
  ['fr', 'Français'],
];

// Switches generated text between English and Quebec French.
export default function LanguageToggle({ value, onChange }) {
  return (
    <div className="inline-flex rounded-lg border overflow-hidden text-xs" role="group" aria-label="Language">
      {LANGUAGES.map(([code, label]) => (
        <button
          key={code}
          type="button"
          onClick={() => onChange(code)}
          aria-pressed={value === code}
          className={`px-2.5 py-1 ${value === code ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

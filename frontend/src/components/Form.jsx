// Small form building blocks shared by the sign-in and setup pages.

export const inputClass =
  'w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500';

export const primaryButton =
  'px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 font-medium';

export const secondaryButton =
  'px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50';

export function Field({ label, htmlFor, help, children }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-sm font-medium text-gray-700 mb-1">
        {label}
      </label>
      {children}
      {help && <p className="text-xs text-gray-500 mt-1">{help}</p>}
    </div>
  );
}

export function Checkbox({ id, checked, onChange, children }) {
  return (
    <div className="flex items-start gap-3">
      <input
        type="checkbox"
        id={id}
        checked={!!checked}
        onChange={(e) => onChange(e.target.checked)}
        className="rounded mt-0.5"
      />
      <label htmlFor={id} className="text-sm text-gray-700">{children}</label>
    </div>
  );
}

export function Choice({ name, value, current, onChange, title, children }) {
  const selected = value === current;
  return (
    <label
      className={`block border rounded-lg p-3 cursor-pointer ${
        selected ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'
      }`}
    >
      <div className="flex items-start gap-3">
        <input
          type="radio"
          name={name}
          value={value}
          checked={selected}
          onChange={() => onChange(value)}
          className="mt-1"
        />
        <div>
          <p className="text-sm font-medium text-gray-800">{title}</p>
          {children && <div className="text-xs text-gray-600 mt-0.5">{children}</div>}
        </div>
      </div>
    </label>
  );
}

export function Message({ error, notice }) {
  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (notice) return <p className="text-sm text-green-700">{notice}</p>;
  return null;
}

// Checkbox list of a source's episodes. Episodes already in the library are
// shown grayed out and can't be picked, so nothing is added twice.
export default function EpisodePicker({ episodes, selected, onChange, emptyText }) {
  const pickable = episodes.filter((ep) => !ep.in_library);

  const toggle = (guid) => {
    const next = new Set(selected);
    if (next.has(guid)) next.delete(guid);
    else next.add(guid);
    onChange(next);
  };

  return (
    <>
      <div className="mt-2 mb-2 flex gap-3 text-xs">
        <button
          type="button"
          onClick={() => onChange(new Set(pickable.map((ep) => ep.guid)))}
          className="text-blue-600 hover:underline"
        >
          Select all
        </button>
        <button
          type="button"
          onClick={() => onChange(new Set())}
          className="text-blue-600 hover:underline"
        >
          Select none
        </button>
        {pickable.length < episodes.length && (
          <span className="text-gray-500">
            {episodes.length - pickable.length} already in your library
          </span>
        )}
      </div>

      <div className="border rounded-lg divide-y max-h-96 overflow-y-auto bg-white">
        {episodes.map((ep) => (
          <label
            key={ep.guid}
            className={`flex items-start gap-3 px-3 py-2 text-sm ${
              ep.in_library ? 'bg-gray-50 text-gray-400 cursor-not-allowed' : 'hover:bg-gray-50 cursor-pointer'
            }`}
          >
            <input
              type="checkbox"
              checked={ep.in_library || selected.has(ep.guid)}
              disabled={ep.in_library}
              onChange={() => toggle(ep.guid)}
              className="mt-1"
            />
            <span>
              <span className={`block ${ep.in_library ? '' : 'text-gray-900'}`}>{ep.title}</span>
              <span className="block text-xs text-gray-500">
                {ep.date && new Date(ep.date).toLocaleDateString()}
                {ep.in_library && <>{ep.date && ' · '}In your library</>}
              </span>
            </span>
          </label>
        ))}
        {episodes.length === 0 && (
          <p className="px-3 py-4 text-sm text-gray-500">{emptyText}</p>
        )}
      </div>
    </>
  );
}

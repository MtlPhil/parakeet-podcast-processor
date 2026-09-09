import { NavLink, Outlet } from 'react-router-dom';

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard' },
  { to: '/podcasts', label: 'Podcasts' },
  { to: '/add', label: 'Add Podcast' },
  { to: '/blogs', label: 'Blog Posts' },
  { to: '/settings', label: 'Settings' },
];

export default function Layout() {
  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="w-56 bg-gray-900 text-gray-300 flex flex-col shrink-0">
        <div className="px-4 py-5 border-b border-gray-700">
          <h1 className="text-lg font-bold text-white tracking-tight">P3</h1>
          <p className="text-xs text-gray-500">Podcast Processor</p>
        </div>
        <nav className="flex-1 py-4">
          {NAV_ITEMS.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `block px-4 py-2 text-sm hover:bg-gray-800 hover:text-white transition-colors ${
                  isActive ? 'bg-gray-800 text-white border-r-2 border-blue-400' : ''
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* Main content */}
      <main className="flex-1 p-6 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}

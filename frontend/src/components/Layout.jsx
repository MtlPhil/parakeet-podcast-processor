import { useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard' },
  { to: '/podcasts', label: 'Podcasts' },
  { to: '/add', label: 'Add Podcast' },
  { to: '/blogs', label: 'Content' },
  { to: '/settings', label: 'Settings' },
];

const linkClass = ({ isActive }) =>
  `block px-4 py-2 text-sm hover:bg-gray-800 hover:text-white transition-colors ${
    isActive ? 'bg-gray-800 text-white border-l-2 lg:border-l-0 lg:border-r-2 border-blue-400' : ''
  }`;

export default function Layout() {
  const [open, setOpen] = useState(false);

  const navLinks = NAV_ITEMS.map(({ to, label }) => (
    <NavLink
      key={to}
      to={to}
      end={to === '/'}
      className={linkClass}
      onClick={() => setOpen(false)}
    >
      {label}
    </NavLink>
  ));

  return (
    <div className="lg:flex lg:min-h-screen">
      {/* Mobile top bar */}
      <header className="lg:hidden sticky top-0 z-20 flex items-center justify-between bg-gray-900 text-white px-4 py-3">
        <div>
          <span className="text-lg font-bold tracking-tight">P3</span>
          <span className="ml-2 text-xs text-gray-500">Podcast Processor</span>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          aria-label="Toggle navigation"
          aria-expanded={open}
          className="p-2 -mr-2"
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            {open ? (
              <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
            ) : (
              <path d="M4 6h16M4 12h16M4 18h16" strokeLinecap="round" />
            )}
          </svg>
        </button>
      </header>

      {/* Mobile dropdown nav */}
      {open && (
        <nav className="lg:hidden bg-gray-900 text-gray-300 pb-2 border-b border-gray-700">
          {navLinks}
        </nav>
      )}

      {/* Desktop sidebar */}
      <aside className="hidden lg:flex w-56 bg-gray-900 text-gray-300 flex-col shrink-0">
        <div className="px-4 py-5 border-b border-gray-700">
          <h1 className="text-lg font-bold text-white tracking-tight">P3</h1>
          <p className="text-xs text-gray-500">Podcast Processor</p>
        </div>
        <nav className="flex-1 py-4">{navLinks}</nav>
      </aside>

      {/* Main content */}
      <main className="flex-1 p-4 sm:p-6 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}

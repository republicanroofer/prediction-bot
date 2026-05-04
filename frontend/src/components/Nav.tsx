type Tab = {
  id: string;
  label: string;
};

const TABS: Tab[] = [
  { id: "overview",  label: "Overview"  },
  { id: "signals",   label: "Signals"   },
  { id: "markets",   label: "Markets"   },
  { id: "whales",    label: "Whales"    },
  { id: "activity",  label: "Activity"  },
  { id: "decisions", label: "Decisions" },
  { id: "telegram",  label: "Alerts"    },
];

type Props = {
  active: string;
  onChange: (id: string) => void;
};

export function Nav({ active, onChange }: Props) {
  return (
    <nav className="flex gap-0.5 sm:gap-1 px-2 sm:px-4 py-1 bg-gray-900 border-b border-gray-800 overflow-x-auto scrollbar-hide">
      {TABS.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`px-2.5 sm:px-4 py-2 text-xs sm:text-sm rounded-t transition-colors whitespace-nowrap shrink-0 ${
            active === t.id
              ? "text-brand-500 border-b-2 border-brand-500 font-semibold"
              : "text-gray-400 hover:text-gray-200"
          }`}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}

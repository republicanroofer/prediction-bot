type Props = {
  label: string;
  value: string | number;
  sub?: string;
  positive?: boolean;
  negative?: boolean;
};

export function StatCard({ label, value, sub, positive, negative }: Props) {
  const valueColor = positive
    ? "text-green-400"
    : negative
    ? "text-red-400"
    : "text-white";

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3 sm:p-4">
      <p className="text-gray-400 text-[10px] sm:text-xs uppercase tracking-wider mb-1">{label}</p>
      <p className={`text-lg sm:text-2xl font-bold ${valueColor}`}>{value}</p>
      {sub && <p className="text-gray-500 text-[10px] sm:text-xs mt-1">{sub}</p>}
    </div>
  );
}

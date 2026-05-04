type Props = { message: string };

export function EmptyState({ message }: Props) {
  return (
    <div className="flex items-center justify-center py-16 text-gray-600 text-sm">
      {message}
    </div>
  );
}

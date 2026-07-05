interface ErrorMessageProps {
  message: string | null;
}

export function ErrorMessage({ message }: ErrorMessageProps) {
  if (!message) {
    return null;
  }

  return (
    <div className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700">
      {message}
    </div>
  );
}

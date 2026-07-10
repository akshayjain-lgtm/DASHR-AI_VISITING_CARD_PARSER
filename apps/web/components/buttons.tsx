export function OBtn({
  children,
  onClick,
  className = "",
  type = "button",
  disabled = false,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  className?: string;
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`bg-[#E65527] text-white px-5 py-2.5 text-sm font-bold hover:bg-[#cf4a1f] transition-colors inline-flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-[#E65527] ${className}`}
    >
      {children}
    </button>
  );
}

export function GBtn({
  children,
  onClick,
  className = "",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  className?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={`border border-black text-black px-5 py-2.5 text-sm font-bold hover:bg-black hover:text-white transition-colors inline-flex items-center gap-2 ${className}`}
    >
      {children}
    </button>
  );
}

export function DBtn({
  children,
  onClick,
  className = "",
  type = "button",
  disabled = false,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  className?: string;
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`bg-red-600 text-white px-5 py-2.5 text-sm font-bold hover:bg-red-700 transition-colors inline-flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-red-600 ${className}`}
    >
      {children}
    </button>
  );
}

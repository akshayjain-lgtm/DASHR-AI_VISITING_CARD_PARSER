export function DashrLogo({
  onClick,
  height = 32,
}: {
  onClick?: () => void;
  height?: number;
}) {
  return (
    <button
      onClick={onClick}
      className="shrink-0 focus:outline-none inline-flex items-center"
      aria-label="DASHR AI home"
    >
      <span
        className="inline-flex items-center gap-1.5 bg-[#E65527] rounded-[3px] px-2.5"
        style={{ height, fontSize: height * 0.42 }}
      >
        <svg
          viewBox="0 0 24 24"
          style={{ width: height * 0.38, height: height * 0.38 }}
          className="shrink-0"
          fill="none"
        >
          <path d="M2 12 14 5v5h8v4h-8v5L2 12z" fill="white" />
        </svg>
        <span className="font-black tracking-tight leading-none whitespace-nowrap">
          <span className="text-white">DASH</span>
          <span className="text-black">R</span>
        </span>
      </span>
    </button>
  );
}

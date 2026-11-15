import Link from "next/link";
export default function NotFound() {
  return (
    <div className="empty">
      페이지를 찾을 수 없습니다. <Link href="/">Fabric으로 돌아가기</Link>
    </div>
  );
}

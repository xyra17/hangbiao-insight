import { Alert, Button } from "antd";
export default function ListError({
  title,
  error,
  retry,
}: {
  title: string;
  error: Error | null;
  retry: () => unknown;
}) {
  return error ? (
    <Alert
      className="wb-list-error"
      type="error"
      showIcon
      title={title}
      description={error.message}
      action={<Button onClick={() => void retry()}>重新读取</Button>}
    />
  ) : null;
}

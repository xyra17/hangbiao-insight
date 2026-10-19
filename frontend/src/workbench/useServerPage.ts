import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, request, type Page } from "./api";
import { isActiveJob } from "./executionStatus";

// The API uses PageNumberPagination with a fixed PAGE_SIZE of 50.
export const SERVER_PAGE_SIZE = 50;
export default function useServerPage<T>({
  key,
  scope,
  path,
  pollActiveJobs = false,
}: {
  key: string;
  scope: string;
  path: string;
  pollActiveJobs?: boolean;
}) {
  const [position, setPosition] = useState({ scope, page: 1 });
  const page = position.scope === scope ? position.page : 1;
  const setPage = (value: number) =>
    setPosition({ scope, page: Math.max(1, value) });
  const queryKey = [key, scope, page];
  const query = useQuery<Page<T>>({
    queryKey,
    queryFn: () =>
      request<Page<T>>(`${path}${path.includes("?") ? "&" : "?"}page=${page}`),
    retry: false,
    refetchInterval: pollActiveJobs
      ? (query) =>
          query.state.status !== "error" &&
          query.state.data?.results.some((row) =>
            isActiveJob((row as { status?: string }).status),
          )
            ? 5000
            : false
      : false,
  });
  useEffect(() => {
    if (query.isFetching) return;
    if (
      page > 1 &&
      query.error instanceof ApiError &&
      query.error.status === 404
    ) {
      // DRF returns 404 when a previously valid last page disappears, even if cached data remains.
      setPosition({ scope, page: 1 });
      return;
    }
    const count = query.data?.count;
    if (count !== undefined) {
      const lastPage = Math.max(1, Math.ceil(count / SERVER_PAGE_SIZE));
      if (page > lastPage) setPosition({ scope, page: lastPage });
    }
  }, [query.data?.count, query.error, query.isFetching, page, scope]);
  return {
    ...query,
    queryKey,
    setPage,
    pagination: {
      current: page,
      pageSize: SERVER_PAGE_SIZE,
      total: query.data?.count ?? 0,
      showSizeChanger: false,
      showTotal: (total: number) => `共 ${total} 条`,
      onChange: setPage,
    },
  };
}

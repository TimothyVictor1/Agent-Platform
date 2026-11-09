import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import ErrorPage from "./error";
it("retries the segment using the installed Next.js error-boundary interface", async () => {
  const retry = vi.fn(); render(<ErrorPage error={new Error("Private details")} retry={retry} />);
  expect(screen.getByRole("alert")).toHaveTextContent("could not be displayed");
  expect(screen.queryByText("Private details")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Try again" })); expect(retry).toHaveBeenCalledOnce();
});

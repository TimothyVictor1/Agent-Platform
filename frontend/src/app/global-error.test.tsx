import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import GlobalError from "./global-error";

it("renders the shared error UI in its own document and retries", async () => {
  const retry = vi.fn();
  render(<GlobalError error={new Error("Private details")} retry={retry} />, { container: document });
  expect(document.documentElement).toHaveAttribute("lang", "en");
  expect(document.body).toContainElement(screen.getByRole("heading", { name: "Something went wrong" }));
  expect(screen.getByRole("alert")).toHaveTextContent("This page could not be displayed. Try loading it again.");
  expect(screen.queryByText("Private details")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(retry).toHaveBeenCalledOnce();
});

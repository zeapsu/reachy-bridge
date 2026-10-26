import { useCallback, useEffect, useState } from "react";
import {
  DIM_KEY,
  PHOTO_MAX_PX,
  WALL_KEY,
  WALL_PHOTO_KEY,
  applyMode,
  applyScheme,
  fitWithin,
  read,
  savedDim,
  savedMode,
  savedScheme,
  savedWall,
  write,
  type Mode,
  type WallId,
} from "../logic/appearance.ts";

export interface Appearance {
  mode: Mode;
  scheme: string;
  wall: WallId;
  photo: string | null;
  dim: number;
  /** Why the last photo couldn't be kept, if it couldn't. */
  photoError: string | null;
  setMode: (mode: Mode) => void;
  setScheme: (scheme: string) => void;
  setWall: (wall: WallId) => void;
  setDim: (dim: number) => void;
  choosePhoto: (file: File) => void;
}

/** Downscale a picked photo to a JPEG data URL that fits browser storage. */
function shrink(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      const { w, h } = fitWithin(img.naturalWidth, img.naturalHeight, PHOTO_MAX_PX);
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return reject(new Error("no canvas"));
      ctx.drawImage(img, 0, 0, w, h);
      resolve(canvas.toDataURL("image/jpeg", 0.82));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("not an image"));
    };
    img.src = url;
  });
}

export function useAppearance(): Appearance {
  const [mode, setModeState] = useState<Mode>(savedMode);
  const [scheme, setSchemeState] = useState(savedScheme);
  const [wall, setWallState] = useState<WallId>(savedWall);
  const [photo, setPhoto] = useState<string | null>(() => read(WALL_PHOTO_KEY));
  const [dim, setDimState] = useState(savedDim);
  const [photoError, setPhotoError] = useState<string | null>(null);

  useEffect(() => applyMode(mode), [mode]);

  const setScheme = useCallback((next: string) => {
    applyScheme(next);
    setSchemeState(next);
  }, []);

  const setWall = useCallback((next: WallId) => {
    write(WALL_KEY, next);
    setWallState(next);
    setPhotoError(null);
  }, []);

  const setDim = useCallback((next: number) => {
    write(DIM_KEY, String(next));
    setDimState(next);
  }, []);

  const choosePhoto = useCallback((file: File) => {
    setPhotoError(null);
    shrink(file)
      .then((data) => {
        setPhoto(data);
        setWallState("photo");
        // Shown either way; it only survives a reload if storage took it.
        if (!write(WALL_PHOTO_KEY, data) || !write(WALL_KEY, "photo")) {
          setPhotoError("This browser couldn't save the photo, so it lasts until you reload.");
        }
      })
      .catch(() => setPhotoError("That file isn't an image this browser can open."));
  }, []);

  return { mode, scheme, wall, photo, dim, photoError, setMode: setModeState, setScheme, setWall, setDim, choosePhoto };
}

import sys
import os

# Ensure the guided-diffusion module is in the Python path
sys.path.append('/kaggle/working/BC_DPM')

print(sys.path)
import resizer
import argparse

import blobfile as bf
import numpy as np
import torch as th
import torch.distributed as dist

from resizer import Resizer
from guided_diffusion import dist_util, logger
from guided_diffusion.script_util import (
    model_and_diffusion_defaults,
    create_model_and_diffusion,
    args_to_dict,
    add_dict_to_argparser,
)
from guided_diffusion.image_datasets import load_data
from torchvision import utils

import math


# added
def load_reference(data_dir, batch_size, image_size, class_cond=False):
    # Ensure the correct data_dir is passed to load_data
    data = load_data(
        data_dir=data_dir,  # Use the passed data_dir
        batch_size=batch_size,
        image_size=image_size,
        class_cond=class_cond,
        deterministic=True,
        random_flip=False,
    )
    for large_batch, model_kwargs in data:
        model_kwargs["ref_img"] = large_batch
        yield model_kwargs


def main():
    args = create_argparser().parse_args()

    # Set up distributed training (if applicable)
    dist_util.setup_dist()
    logger.configure(dir=args.save_dir)

    logger.log("creating model...")
    # Create model and diffusion using the provided arguments
    model, diffusion = create_model_and_diffusion(
        **args_to_dict(args, model_and_diffusion_defaults().keys())
    )
    model.load_state_dict(
        dist_util.load_state_dict(args.model_path, map_location="cpu")
    )
    model.to(dist_util.dev())
    if args.use_fp16:
        model.convert_to_fp16()

    # Convert the model to float16 precision if using fp16
    if args.use_fp16:
        model = model.half()  # Convert model to float16
    model.eval()

    logger.log("creating resizers...")
    assert math.log(args.down_N, 2).is_integer()

    shape = (args.batch_size, 3, args.image_size, args.image_size)
    shape_d = (args.batch_size, 3, int(args.image_size / args.down_N), int(args.image_size / args.down_N))
    down = Resizer(shape, 1 / args.down_N).to(next(model.parameters()).device)
    up = Resizer(shape_d, args.down_N).to(next(model.parameters()).device)
    resizers = (down, up)

    logger.log("loading data...")
    # Pass data_dir correctly here
    data = load_reference(
        args.data_dir,  # Pass the data_dir from the arguments
        args.batch_size,
        image_size=args.image_size,
        class_cond=args.class_cond,
    )

    logger.log("creating samples...")
    count = 0
    while count * args.batch_size < args.num_samples:
        model_kwargs = next(data)
        model_kwargs = {k: v.to(dist_util.dev()) for k, v in model_kwargs.items()}
        sample = diffusion.p_sample_loop(
            model,
            (args.batch_size, 3, args.image_size, args.image_size),
            clip_denoised=args.clip_denoised,
            model_kwargs=model_kwargs,
            resizers=resizers,
            range_t=args.range_t,
        )

        for i in range(args.batch_size):
            out_path = os.path.join(logger.get_dir(),
                                    f"{str(count * args.batch_size + i).zfill(5)}.png")
            utils.save_image(
                sample[i].unsqueeze(0),
                out_path,
                nrow=1,
                normalize=True,
                range=(-1, 1),
            )

        count += 1
        logger.log(f"created {count * args.batch_size} samples")

    dist.barrier()
    logger.log("sampling complete")


def create_argparser():
    """
    Create an argument parser with default arguments for training.
    """
    defaults = dict(
        clip_denoised=True,
        num_samples=3820,
        batch_size=1,
        down_N=32,
        range_t=0,
        use_ddim=False,
        base_samples="",
        model_path="/kaggle/working/model.pth",  # Path to your model
        save_dir="/kaggle/working",  # Path to save output
        save_latents=False,
        lambda_a=0.2,
        data_dir="",  # This should be specified when running the script
        class_cond=False,  # Adjust if you want class-conditioned sampling
        image_size=256,  # Example image size, change as needed
    )
    defaults.update(model_and_diffusion_defaults())
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, defaults)
    return parser


if __name__ == "__main__":
    main()

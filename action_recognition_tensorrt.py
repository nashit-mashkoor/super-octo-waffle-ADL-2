import os
import sys
from collections import deque
import traceback
import pycuda.driver as cuda
import pycuda.autoinit
import numpy as np
import tensorrt as trt
import cv2
import time
# python3 action_recognition_tensorrt.py --stream /home/nvidia/ADL-2/test1.mp4 --model /home/nvidia/ADL-2/resnet-18-kinetcis-moments.onnx --fp16 --frameskip 2
# python3 action_recognition_tensorrt.py --stream /home/nvidia/ADL-2/test1.mp4 --model /home/nvidia/ADL-2/resnet-18-kinetcis-moments.onnx --fp16 --frameskip 2
from opts import parse_arguments

EXPLICIT_BATCH = 1 << (int)(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)

CLASSES = open('/home/nvidia/ADL-2/action_recognition_kinetics_moments.txt').read().strip().split("\n")

DURATION = 16
INPUT_SIZE = 112

# logger to sourceture errors, warnings, and other information during the build and inference phases
TRT_LOGGER = trt.Logger()

class HostDeviceMem(object):
    def __init__(self, host_mem, device_mem):
        self.host = host_mem
        self.device = device_mem

    def __str__(self):
        return "Host:\n" + str(self.host) + "\nDevice:\n" + str(self.device)

    def __repr__(self):
        return self.__str__()

class TensorRTInference:

    def get_engine(self, onnx_file_path, engine_file_path,
        workspace, batch_size, fp16):

        precision = 'fp32'
        if fp16:
            precision = 'fp16'

        def build_engine():

            builder = trt.Builder(TRT_LOGGER)
            config = builder.create_builder_config()
            network = builder.create_network(EXPLICIT_BATCH)
            parser = trt.OnnxParser(network, TRT_LOGGER)
            runtime = trt.Runtime(TRT_LOGGER)

            # allow TensorRT to use up to 1GB of GPU memory for tactic selection
            config.max_workspace_size = workspace
            # we have only one image in batch
            builder.max_batch_size = batch_size
            # use FP16 mode if possible
            if precision == 'fp16' and builder.platform_has_fast_fp16:
                config.set_flag(trt.BuilderFlag.FP16)

            # parse ONNX
            with open(onnx_file_path, 'rb') as model:
                print('Beginning ONNX file parsing')
                if not parser.parse(model.read()):
                    print('[Engine] ERROR: Failed to parse ONNX file')
                    for error in range(parser.num_errors):
                        print(parser.get_error(error))
                    return None
                else:
                    print('[Engine] Completed parsing of ONNX file')

            # generate TensorRT engine optimized for the target platform
            print('[Engine] Building an engine')
            plan = builder.build_serialized_network(network, config)
            engine = runtime.deserialize_cuda_engine(plan)
            # engine = builder.build_cuda_engine(network, config)

            print('[Engine] Completed creating Engine')

            with open(engine_file_path.format(batch_size, precision), 'wb') as f:
                    f.write(engine.serialize())

            print('[Engine] Engine burned on disk as {}'.format(engine_file_path.format(batch_size, precision)))

            return engine

        if os.path.exists(engine_file_path.format(batch_size, precision)):
            # If a serialized engine exists, use it instead of building an engine.
            print("[Engine] Reading engine from file {}".format(engine_file_path.format(batch_size, precision)))

            with open(engine_file_path.format(batch_size, precision), "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
                return runtime.deserialize_cuda_engine(f.read())

        else:
            return build_engine()

    def allocate_buffers(self, engine):
        inputs = list()
        outputs = list()
        bindings = list()
        for binding in engine:
            size = trt.volume(engine.get_binding_shape(binding)) * engine.max_batch_size * np.dtype(np.float32).itemsize
            dtype = trt.nptype(engine.get_binding_dtype(binding))
            # Allocate host and device buffers
            host_mem = cuda.pagelocked_empty(size, dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            # Append the device buffer to device bindings.
            bindings.append(int(device_mem))
            # Append to the appropriate list.
            if engine.binding_is_input(binding):
                inputs.append(HostDeviceMem(host_mem, device_mem))
            else:
                outputs.append(HostDeviceMem(host_mem, device_mem))
        return inputs, outputs, bindings

    def run_inference(self, host_input):

        inputs, outputs, bindings = self.allocate_buffers(self.engine)
        inputs[0].host = host_input

        # Transfer input data to the GPU.
        [cuda.memcpy_htod_async(inp.device, inp.host, self.stream) for inp in inputs]
        # Run inference.
        self.context.execute_async_v2(bindings=bindings, stream_handle=self.stream.handle)
        # Transfer predictions back from the GPU.
        [cuda.memcpy_dtoh_async(out.host, out.device, self.stream) for out in outputs]
        # Synchronize the stream
        self.stream.synchronize()
        # Return only the host outputs.

        outputs = [out.host for out in outputs]

        return CLASSES[np.argmax(outputs)]

    def __init__(self, onnx_file_path, engine_file_path,
        workspace=1<<30, batch_size=1, fp16=False):

        # Build Engine
        self.engine = self.get_engine(onnx_file_path, engine_file_path,
        workspace, batch_size, fp16)

        # Set Context
        self.context = self.engine.create_execution_context()

        # Create stream to copy inputs/outputs
        self.stream = cuda.Stream()

if __name__ == '__main__':

    opt = parse_arguments()

    if opt.stream == '':
        print('[Error] Please provide a valid path --stream.')
        sys.exit(0)

    if opt.model == '':
        print('[Error] Please provide a valid path --model.')
        sys.exit(0)


    ONNX_FILE_PATH = opt.model
    ENGINE_FILE_PATH = ONNX_FILE_PATH + '_b{}_{}.engine'

    trt_inference = TensorRTInference(ONNX_FILE_PATH, ENGINE_FILE_PATH,
                          1<<30, 1, opt.fp16)

    if not opt.save_output == '':
        writer = cv2.VideoWriter(opt.save_output,
                    cv2.VideoWriter_fourcc(*'MJPG'),
                    60, (1920, 1080))

    source = cv2.VideoCapture(0 if opt.stream == 'webcam' else opt.stream)

    frames = deque(maxlen=DURATION)
    skip = 0
    result = ''
    inferencetime = 0

    while True:
        ret, frame = source.read()

        if not ret:
            break

        frame = cv2.resize(frame, (1920, 1080))
        skip += 1
        if skip % opt.frameskip == 0:
            skip = 0

            frames.append(frame)

            if not len(frames) < DURATION:
                blob = cv2.dnn.blobFromImages(frames, (1.0/255),
                    (INPUT_SIZE, INPUT_SIZE), (110.79, 103.3, 96.26),
                    swapRB=True, crop=False)
                blob = np.transpose(blob, (1, 0, 2, 3))
                blob = np.expand_dims(blob, axis=0)
                blob = np.ascontiguousarray(blob)

                start = time.time()
                result = trt_inference.run_inference(blob)
                inferencetime = round(time.time() - start, 4)
                print(result, end='\r')
                print('Inference Time: {} ms'.format(inferencetime), end='\r')

        overlay = frame.copy()
        display = frame.copy()

        cv2.rectangle(overlay, (560, 850), (1360, 1000), (0, 0, 0), -1)
        cv2.putText(overlay, 'Inference Time: {} s'.format(inferencetime), (600, 900), cv2.FONT_HERSHEY_COMPLEX,
            1.25, (255, 255, 255), 2)
        cv2.putText(overlay, 'Output: {}'.format(result), (600, 950), cv2.FONT_HERSHEY_COMPLEX,
            1.25, (0, 255, 0), 2)

        cv2.addWeighted(overlay, 0.7, display, 1 - 0.7, 0, display)

        # cv2.imshow('Output', display)

        if not opt.save_output == '':
            writer.write(display)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    source.release()
    writer.release()
    # cv2.destroyAllWindows()

import torch
from torchvision import datasets, transforms
from torch.utils.data import DataLoader
import numpy as np
from datetime import datetime
from BitNetMCU import QuantizedModel
from models import FCMNIST
import math
import matplotlib.pyplot as plt
import argparse
import yaml
import seaborn as sns

# Export quantized model from saved checkpoint
# cpldcpu 2024-04-14
# Note: Hyperparameters are used to generated the filename
#---------------------------------------------

showplots = True # display plots with statistics

def create_run_name(hyperparameters):
    runname = hyperparameters["runtag"] + hyperparameters["scheduler"] + '_lr' + str(hyperparameters["learning_rate"]) + ('_Aug' if hyperparameters["augmentation"] else '') + '_BitMnist_' + hyperparameters["WScale"] + "_" +hyperparameters["QuantType"] + "_" + hyperparameters["NormType"] + "_width" + str(hyperparameters["network_width1"]) + "_" + str(hyperparameters["network_width2"]) + "_" + str(hyperparameters["network_width3"])  + "_bs" + str(hyperparameters["batch_size"]) + "_epochs" + str(hyperparameters["num_epochs"])
    hyperparameters["runname"] = runname
    return runname


def export_to_hfile(quantized_model, filename, runname):
    """
    Exports the quantized model to an Ansi-C header file.

    Parameters:
    filename (str): The name of the header file to which the quantized model will be exported.

    Note:
    This method currently only supports binary quantization. 
    """

    if not quantized_model.quantized_model:
        raise ValueError("quantized_model is empty or None")

    # determine maximum number of activations per layer
    max_n_activations = max([layer['outgoing_weights'] for layer in quantized_model.quantized_model])
    

    with open(filename, 'w') as f:
        f.write(f'// Automatically generated header file\n')
        f.write(f'// Date: {datetime.now()}\n')
        f.write(f'// Quantized model exported from {runname}.pth\n')
        f.write('// Generated by exportquant.py\n\n')

        f.write('#include <stdint.h>\n\n')

        f.write('#ifndef BITNETMCU_MODEL_H\n')
        f.write('#define BITNETMCU_MODEL_H\n\n')

        f.write(f'// Number of layers\n')
        f.write(f'#define NUM_LAYERS {len(quantized_model.quantized_model)}\n\n')
        f.write(f'// Maximum number of activations per layer\n')
        f.write(f'#define MAX_N_ACTIVATIONS {max_n_activations}\n\n')

        for layer_info in quantized_model.quantized_model:
            layer= f'L{layer_info["layer_order"]}'
            incoming_weights = layer_info['incoming_weights']
            outgoing_weights = layer_info['outgoing_weights']
            bpw = layer_info['bpw']
            weights = np.array(layer_info['quantized_weights'])
            quantization_type = layer_info['quantization_type']

            if (bpw*incoming_weights%32) != 0:
                raise ValueError(f"Size mismatch: Incoming weights must be packed to 32bit boundary. Incoming weights: {incoming_weights} Bit per weight: {bpw} Total bits: {bpw*incoming_weights}")

            print(f'Layer: {layer} Quantization type: <{quantization_type}>, Bits per weight: {bpw}, Num. incoming: {incoming_weights},  Num outgoing: {outgoing_weights}')
            
            data_type = np.uint32
            
            if quantization_type == 'Binary':
                encoded_weights = np.where(weights == -1, 0, 1)
                QuantID = 1
            elif quantization_type == '2bitsym': # encoding -1.5 -> 11, -0.5 -> 10, 0.5 -> 00, 1.5 -> 01 (one complement with offset)
                encoded_weights = ((weights < 0).astype(data_type) << 1) | (np.floor(np.abs(weights))).astype(data_type)  # use bitwise operations to encode the weights
                QuantID = 2
            elif quantization_type == '4bitsym': 
                encoded_weights = ((weights < 0).astype(data_type) << 3) | (np.floor(np.abs(weights))).astype(data_type)  # use bitwise operations to encode the weights
                QuantID = 4
            elif quantization_type == '4bit': 
                encoded_weights = np.floor(weights).astype(int) & 15  # twos complement encoding
                QuantID =  8 + 4
            elif quantization_type == 'FP130': # FP1.3.0 encoding (sign * 2^exp)
                encoded_weights = ((weights < 0).astype(data_type) << 3) | (np.floor(np.log2(np.abs(weights)))).astype(data_type)  
                QuantID = 16 + 4
            else:
                print(f'Skipping layer {layer} with quantization type {quantization_type} and {bpw} bits per weight. Quantization type not supported.')

            # pack bits into 32 bit words
            weight_per_word = 32 // bpw 
            reshaped_array = encoded_weights.reshape(-1, weight_per_word)
            
            bit_positions = 32 - bpw - np.arange(weight_per_word, dtype=data_type) * bpw
            packed_weights = np.bitwise_or.reduce(reshaped_array << bit_positions, axis=1).view(data_type)
            
            # print(f'weights: {weights.shape} {weights.flatten()[0:16]}')
            # print(f'Encoded weights: {encoded_weights.shape} {encoded_weights.flatten()[0:16]}')
            # print(f'Packed weights: {packed_weights.shape} {", ".join(map(lambda x: hex(x), packed_weights.flatten()[0:4]))}')

            # Write layer order, shape, shiftright and weights to the file
            f.write(f'// Layer: {layer}\n')
            f.write(f'// QuantType: {quantization_type}\n')

            f.write(f'#define {layer}_active\n')
            f.write(f'#define {layer}_bitperweight {QuantID}\n')
            f.write(f'#define {layer}_incoming_weights {incoming_weights}\n')
            f.write(f'#define {layer}_outgoing_weights {outgoing_weights}\n')

            f.write(f'const uint32_t {layer}_weights[] = {{')         
            for i,data in enumerate(packed_weights.flatten()):
                if i&7 ==0:
                    f.write('\n\t')
                f.write(f'0x{data:08x},')
            f.write('\n}; //first channel is topmost bit\n\n')
           
        f.write('#endif\n')

def plot_test_images(test_loader):
    dataiter = iter(test_loader)
    images, labels = next(dataiter)

    fig, axes = plt.subplots(5, 5, figsize=(8, 8))

    for i, ax in enumerate(axes.flat):
        ax.imshow(images[i].numpy().squeeze(), cmap='gray')
        ax.set_title(f'Label: {labels[i]}')
        ax.axis('off')

    plt.tight_layout()
    plt.show() 

def print_stats(quantized_model):
    for layer_info in quantized_model.quantized_model:
        weights = np.array(layer_info['quantized_weights'])
        print()
        print(f'Layer: {layer_info["layer_order"]}, Max: {np.max(weights)}, Min: {np.min(weights)}, Mean: {np.mean(weights)}, Std: {np.std(weights)}')

        values, counts = np.unique(weights, return_counts=True)
        probabilities = counts / np.sum(counts)

        print(f'Values: {values}')
        print(f'Percent: {(probabilities * 100)}')

        number_of_codes = 2**layer_info['bpw'] 
        entropy = -np.sum(probabilities * np.log2(probabilities))
        print(f'Entropy: {entropy:.2f} bits. Code capacity used: {entropy / np.log2(number_of_codes) * 100} %')
  

def plot_statistics(quantized_model):
    # Step 1: Extract the weights of the first layer
    first_layer_weights = np.array(quantized_model.quantized_model[0]['quantized_weights'])

    # Step 2: Reshape the weights into a 16x16 grid
    reshaped_weights = first_layer_weights.reshape(28, 28, -1)
    print(reshaped_weights.shape)
    # Step 3: Calculate the variance of each channel
    variances = np.var(reshaped_weights, axis=-1)

    # Calculate the mean of each channel
    means = np.mean(reshaped_weights, axis=-1)

    # Create a figure with 2 subplots: one for variance, one for mean
    fig, axs = plt.subplots(1, 2, figsize=(12, 6))

    # Plot the variance
    axs[0].imshow(variances, cmap='hot', interpolation='nearest')
    axs[0].set_title('Variance vs Channel')
    fig.colorbar(plt.cm.ScalarMappable(cmap='hot'), ax=axs[0], label='Variance')

    # Plot the mean
    im = axs[1].imshow(means, cmap='hot', interpolation='nearest')
    axs[1].set_title('Mean vs Channel')
    fig.colorbar(im, ax=axs[1], label='Mean')

    # Display the plot
    plt.show(block=False)    

        
def plot_weights(quantized_model):
    # Step 1: Extract the weights of the first layer
    first_layer_weights = np.array(quantized_model.quantized_model[0]['quantized_weights'])

    # Step 2: Reshape the weights into a 16x16 grid for each output channel
    reshaped_weights = first_layer_weights.reshape(-1, 28, 28)

    # Calculate the number of output channels
    num_channels = reshaped_weights.shape[0]

    # Calculate the number of rows and columns for the subplots
    num_cols = int(math.sqrt(num_channels))
    num_rows = num_channels // num_cols
    if num_channels % num_cols != 0:
        num_rows += 1

    # Step 3: Create a figure with a grid of subplots, one for each output channel
    fig, axs = plt.subplots(num_rows, num_cols, figsize=(8, 8))

    # Step 4: For each output channel, plot the weights in the corresponding subplot
    for i in range(num_cols*num_rows):
        row = i // num_cols
        col = i % num_cols
        if i < num_channels:
            axs[row, col].imshow(reshaped_weights[i], cmap='hot', interpolation='nearest')
        axs[row, col].axis('off')  # Turn off axis for each subplot
    # Reduce the gaps between the subplots
    # plt.subplots_adjust(wspace=-0.10, hspace=-0.10)
    # Display the plot
    plt.tight_layout()  # This will ensure the subplots do not overlap
    plt.show(block=False)

def plot_weight_histograms(quantized_model):
    fig = plt.figure(figsize=(10, 10))

    for layer_index, layer in enumerate(quantized_model.quantized_model):
        layer_weights = np.array(layer['quantized_weights'])
        bpw = layer['bpw']

        flattened_weights = layer_weights.flatten()

        ax = fig.add_subplot(len(quantized_model.quantized_model), 1, layer_index + 1)

        # ax.hist(flattened_weights, width=1, bins='auto')
        sns.histplot(flattened_weights, bins=2**bpw, ax=ax, kde=True)
        ax.set_title(f'Layer {layer_index+1} Weight Distribution')

    plt.tight_layout()  
    plt.show(block=False)

class ToBinary:
    def __init__(self, threshold=0.5):
        self.threshold = threshold
    
    def __call__(self, img):
        # Apply the threshold and convert to binary (0 or 1)
        # print(type(img))
        # print((img>self.threshold).float())
        return (img > self.threshold).float()
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Training script')
    parser.add_argument('--params', type=str, help='Name of the parameter file', default='trainingparameters.yaml')
    
    args = parser.parse_args()
    
    if args.params:
        paramname = args.params
    else:
        paramname = 'trainingparameters.yaml'

    print(f'Load parameters from file: {paramname}')
    with open(paramname) as f:
        hyperparameters = yaml.safe_load(f)

    # main
    runname= create_run_name(hyperparameters)
    print(runname)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load the MNIST dataset
    transform = transforms.Compose([
        # transforms.Resize((28, 28)),  # Resize images to 16x16
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,)),
        ToBinary(0.5)
    ])

    train_data = datasets.MNIST(root='data', train=True, transform=transform, download=True)
    test_data = datasets.MNIST(root='data', train=False, transform=transform)
    # Create data loaders
    test_loader = DataLoader(test_data, batch_size=hyperparameters["batch_size"], shuffle=False)

    # plot_test_images(test_loader)

    # Initialize the network and optimizer
    model = FCMNIST(
        network_width1=hyperparameters["network_width1"], 
        network_width2=hyperparameters["network_width2"], 
        network_width3=hyperparameters["network_width3"], 
        QuantType=hyperparameters["QuantType"], 
        NormType=hyperparameters["NormType"],
        WScale=hyperparameters["WScale"],
        quantscale=hyperparameters["quantscale"]
    ).to(device)

    print('Loading model...')    
    try:
        model.load_state_dict(torch.load(f'modeldata/{runname}.pth'))
    except FileNotFoundError:
        print(f"The file 'modeldata/{runname}.pth' does not exist.")
        exit()

    print('Inference using the original model...')
    correct = 0
    total = 0
    test_loss = []
    with torch.no_grad():
        for images, labels in test_loader:
            images, labels = images.to(device), labels.to(device)        
            outputs = model(images)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    testaccuracy = correct / total * 100
    print(f'Accuracy/Test of trained model: {testaccuracy} %')

    print('Quantizing model...')
    # Quantize the model
    quantized_model = QuantizedModel(model, quantscale=hyperparameters["quantscale"])

    # Print statistics
    print_stats(quantized_model)

    if showplots:
        plot_weights(quantized_model)
        # plot_statistics(quantized_model)
        plot_weight_histograms(quantized_model)
        # plot_test_images(test_loader)

    print(f'Total number of bits: {quantized_model.totalbits()} ({quantized_model.totalbits()/8/1024} kbytes)')

    # Inference using the quantized model
    print ("inference of quantized model")

    # Initialize counters
    total_correct_predictions = 0
    total_samples = 0

    # Iterate over the test data
    for input_data, labels in test_loader:
        # Reshape and convert to numpy
        input_data = input_data.view(input_data.size(0), -1).cpu().numpy()

        labels = labels.cpu().numpy()

        # Inference
        result = quantized_model.inference_quantized(input_data)

        # Get predictions
        predict = np.argmax(result, axis=1)

        # Calculate the fraction of correct predictions for this batch
        correct_predictions = (predict == labels).sum()

        # Update counters
        total_correct_predictions += correct_predictions  # Multiply by batch size
        total_samples += input_data.shape[0]

    # Calculate and print the overall fraction of correct predictions
    overall_correct_predictions = total_correct_predictions / total_samples

    print('Accuracy/Test of quantized model:', overall_correct_predictions * 100, '%') 

    print("Exporting model to header file")
    # export the quantized model to a header file
    # export_to_hfile(quantized_model, f'{exportfolder}/{runname}.h')
    export_to_hfile(quantized_model, f'BitNetMCU_model28.h',runname)
    
    if showplots:
        plt.show()
